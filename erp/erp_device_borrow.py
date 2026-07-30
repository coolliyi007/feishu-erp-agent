"""
erp_device_borrow.py — 飞书"设备领用/归还"审批通过后自动操作 ERP

共用 erp_asset.py 里的：登录、滑块、ERP锁、飞书消息发送
本脚本负责：解析领用/归还审批表单 → 在 ERP 里完成对应操作

用法：
    py erp_device_borrow.py <instance_code>
"""

import asyncio, json, sys, traceback, os
from erp_asset import (
    acquire_erp_lock, release_erp_lock, login_erp, get_frame_by_url,
    send_message, get_user_email, get_approval_detail, APP_ID, APP_SECRET,
    MY_CHAT_ID, ERP_URL, navigate_to_asset, search_device
)
from playwright.async_api import async_playwright

APPROVAL_CODE = os.environ.get("APPROVAL_CODE_DEVICE_BORROW", "")

# ERP 领用/归还 功能入口名称
BORROW_MENU  = "设备领用"
RETURN_MENU  = "设备归还"
BORROW_FRAME = "deviceBorrow"
RETURN_FRAME = "deviceReturn"


def parse_borrow_form(form):
    """
    解析领用/归还表单，返回：
    {
      "type": "领用" | "归还",
      "devices": [{"device_code": ..., "receiver_id": ..., "location": ...}]
    }
    """
    op_type = "领用"
    devices = []
    for field in form:
        name = field.get("name", "")
        value = field.get("value")

        if name == "操作类型":
            op_type = value if value in ("领用", "归还") else "领用"

        if name in ("设备明细", "领用设备明细", "归还设备明细") and isinstance(value, list):
            for row in value:
                device_code = receiver_id = location = None
                for item in (row if isinstance(row, list) else []):
                    n, v = item.get("name"), item.get("value")
                    if n == "设备编号":   device_code = v
                    elif n == "接收人":  receiver_id = v[0] if isinstance(v, list) and v else v
                    elif n in ("办公大厅", "位置", "存放位置"): location = v
                if device_code:
                    devices.append({"device_code": device_code, "receiver_id": receiver_id, "location": location})
    return {"type": op_type, "devices": devices}


async def do_borrow(page, device_code, receiver_email, location):
    """ERP 领用操作"""
    # 进入领用页面
    await page.click(f'text={BORROW_MENU}')
    await asyncio.sleep(2)
    await page.wait_for_load_state("networkidle")

    frame = get_frame_by_url(page, BORROW_FRAME)
    if not frame:
        return False, "未找到领用页面"

    # 填设备编号 → 查询
    await frame.evaluate("v => $('#deviceCode').textbox('setValue', v)", device_code)
    await asyncio.sleep(1)
    await frame.locator('a[onclick*="search"]').first.click()
    await asyncio.sleep(2)

    # 选第一行
    rows = frame.locator('.datagrid-btable tbody tr')
    for i in range(await rows.count()):
        row = rows.nth(i)
        if 'height: 1px' not in (await row.get_attribute('style') or ''):
            await row.click()
            await asyncio.sleep(1)
            break
    else:
        return False, "设备未找到"

    # 填接收人邮箱
    await frame.evaluate("v => $('#receiverEmail').textbox('setValue', v)", receiver_email)
    await asyncio.sleep(1)

    # 填位置
    if location:
        await frame.evaluate("v => $('#location').textbox('setValue', v)", location)
        await asyncio.sleep(1)

    # 提交
    await frame.evaluate("() => submitBorrow()")
    await asyncio.sleep(2)

    for target in [page, frame]:
        try:
            await target.wait_for_selector('text=确定', timeout=5000)
            await target.locator('text=确定').click()
            return True, ""
        except:
            pass
    return True, ""


async def do_return(page, device_code):
    """ERP 归还操作"""
    await page.click(f'text={RETURN_MENU}')
    await asyncio.sleep(2)
    await page.wait_for_load_state("networkidle")

    frame = get_frame_by_url(page, RETURN_FRAME)
    if not frame:
        return False, "未找到归还页面"

    await frame.evaluate("v => $('#deviceCode').textbox('setValue', v)", device_code)
    await asyncio.sleep(1)
    await frame.locator('a[onclick*="search"]').first.click()
    await asyncio.sleep(2)

    rows = frame.locator('.datagrid-btable tbody tr')
    for i in range(await rows.count()):
        row = rows.nth(i)
        if 'height: 1px' not in (await row.get_attribute('style') or ''):
            await row.click()
            await asyncio.sleep(1)
            break
    else:
        return False, "设备未找到"

    await frame.evaluate("() => submitReturn()")
    await asyncio.sleep(2)

    for target in [page, frame]:
        try:
            await target.wait_for_selector('text=确定', timeout=5000)
            await target.locator('text=确定').click()
            return True, ""
        except:
            pass
    return True, ""


async def process_borrow_batch(parsed, instance_code):
    op_type = parsed["type"]
    devices_raw = parsed["devices"]

    lock = acquire_erp_lock()
    if not lock:
        send_message(MY_CHAT_ID, f"❌ ERP锁等待超时\n{instance_code}")
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=["--no-proxy-server"])
            page = await (await browser.new_context(viewport={"width": 1920, "height": 1080})).new_page()

            if not await login_erp(page):
                await browser.close()
                send_message(MY_CHAT_ID, f"❌ ERP登录失败\n{instance_code}")
                return

            total = len(devices_raw)
            for i, dev in enumerate(devices_raw, 1):
                device_code = dev["device_code"]

                # 归还不需要接收人，领用需要
                if op_type == "归还":
                    # 每台前回主页
                    await page.goto(ERP_URL.split("?")[0].replace("/auth/login", "").replace("cpssso", "cps"))
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                    ok, err = await do_return(page, device_code)
                    msg_ok = f"✅ [{i}/{total}] 归还完成\n设备：{device_code}"
                else:
                    receiver_id = dev.get("receiver_id")
                    email = receiver_id if "@" in str(receiver_id) else get_user_email(receiver_id)
                    location = dev.get("location", "")
                    await page.goto(ERP_URL.split("?")[0].replace("/auth/login", "").replace("cpssso", "cps"))
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                    ok, err = await do_borrow(page, device_code, email, location)
                    msg_ok = f"✅ [{i}/{total}] 领用完成\n设备：{device_code}\n接收人：{email}"

                send_message(MY_CHAT_ID, msg_ok if ok else f"❌ [{i}/{total}] {op_type}失败\n设备：{device_code}\n原因：{err}")
                await asyncio.sleep(2)

            await browser.close()
    finally:
        release_erp_lock(lock)


def handle_approval(instance_code):
    detail = get_approval_detail(instance_code)
    if not detail:
        send_message(MY_CHAT_ID, f"⚠️ 获取审批详情失败：{instance_code}")
        return

    form = json.loads(detail.form) if isinstance(detail.form, str) else detail.form
    parsed = parse_borrow_form(form)

    if not parsed["devices"]:
        send_message(MY_CHAT_ID, f"⚠️ 审批单未找到设备明细：{instance_code}")
        return

    send_message(MY_CHAT_ID,
        f"🔧 开始处理{parsed['type']}审批 {instance_code}\n共 {len(parsed['devices'])} 台设备")
    asyncio.run(process_borrow_batch(parsed, instance_code))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: py erp_device_borrow.py <instance_code>")
        sys.exit(2)
    instance_code = sys.argv[1]
    try:
        handle_approval(instance_code)
    except Exception as e:
        traceback.print_exc()
        send_message(MY_CHAT_ID, f"❌ ERP设备领用/归还异常\n{instance_code}\n{e}")
        sys.exit(1)
