"""
erp_asset.py — 飞书审批通过后自动完成 ERP 资产变更

流程：
    飞书审批通过 → listener.js 调用本脚本 → 拉取审批详情 →
    Playwright 登录 ERP → 逐台设备修改接收人/位置 → 飞书通知结果

用法：
    py erp_asset.py <instance_code>
"""

import asyncio, base64, random, json, io, sys, time, socket, os
import requests, traceback
from PIL.Image import open as pil_open
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.api.approval.v4 import GetInstanceRequest
from playwright.async_api import async_playwright

# ── ERP 全局互斥锁（TCP 端口占位） ───────────────────────────────────────────
# erp_asset.py 与 erp_device_borrow.py 共用同一个 ERP 账号，
# 并发会导致 session 被踢、左侧菜单不可见，用端口锁串行化。
ERP_LOCK_PORT = 47394

def acquire_erp_lock(timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(('127.0.0.1', ERP_LOCK_PORT))
            s.listen(1)
            print("[ERP锁] 已获取")
            return s
        except OSError:
            s.close()
            print("[ERP锁] 等待其他 ERP 进程完成…")
            time.sleep(5)
    print("[ERP锁] 等待超时，放弃")
    return None

def release_erp_lock(s):
    try: s.close()
    except: pass

# ── 配置（从环境变量读取，本地开发复制 .env.example 改名为 .env）──────────
APP_ID        = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET    = os.environ.get("FEISHU_APP_SECRET", "")
APPROVAL_CODE = os.environ.get("APPROVAL_CODE_ASSET_CHANGE", "")
MY_CHAT_ID    = os.environ.get("FEISHU_CHAT_ID", "")

ERP_URL   = os.environ.get("ERP_URL", "")
USERNAME  = os.environ.get("ERP_USERNAME", "")
PASSWORD  = os.environ.get("ERP_PASSWORD", "")

CJY_USERNAME = os.environ.get("CHAOJIYING_USER", "")
CJY_PASSWORD = os.environ.get("CHAOJIYING_PASS", "")
CJY_SOFT_ID  = os.environ.get("CHAOJIYING_SOFTID", "")

# ERP 内部页面 URL 特征（用于 iframe 定位）
ASSET_FRAME_URL    = "your-erp-domain/asset-management"
MODIFY_FRAME_URL   = "assetInformationUpdate"
SELECT_USER_FRAME_URL = "selectUsePage"
# ─────────────────────────────────────────────────────────────────────────────

lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()


def send_message(chat_id, text):
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id).msg_type("text")
            .content(json.dumps({"text": text})).build()
        ).build()
    resp = lark_client.im.v1.message.create(req)
    print(f"发送结果: {resp.success()}")


def get_user_email(user_id):
    token = _get_tenant_token()
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/contact/v3/users/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
        params={"user_id_type": "user_id", "user_fields": "enterprise_email,email"}
    )
    user = resp.json().get("data", {}).get("user", {})
    return user.get("enterprise_email") or user.get("email")


def _get_tenant_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}
    )
    return resp.json().get("tenant_access_token")


def get_approval_detail(instance_code):
    req = GetInstanceRequest.builder().instance_id(instance_code).build()
    response = lark_client.approval.v4.instance.get(req)
    return response.data if response.success() else None


def parse_approval_form(form):
    """从审批表单里解析设备列表：[{device_code, receiver_id, location}]"""
    devices = []
    for field in form:
        if field.get('name') == '资产明细（在职-变更、归还）' and isinstance(field.get('value'), list):
            for row in field['value']:
                device_code = receiver_id = location = None
                for item in (row if isinstance(row, list) else []):
                    n, v = item.get('name'), item.get('value')
                    if n == '设备编号':   device_code = v
                    elif n == '接收人':  receiver_id = v[0] if isinstance(v, list) and v else v
                    elif n == '办公大厅': location = v
                if device_code:
                    devices.append({'device_code': device_code, 'receiver_id': receiver_id, 'location': location})
    return devices


# ── 验证码识别（超级鹰打码平台） ──────────────────────────────────────────────
def recognize_captcha(img_bytes: bytes) -> dict:
    img_base64 = base64.b64encode(img_bytes).decode()
    for retry in range(3):
        try:
            response = requests.post(
                "https://upload.chaojiying.net/Upload/Processing.php",
                data={"user": CJY_USERNAME, "pass": CJY_PASSWORD,
                      "softid": CJY_SOFT_ID, "codetype": "9902",
                      "file_base64": img_base64},
                timeout=30,
                proxies={"http": None, "https": None},  # 直连，不走系统代理
            )
            return response.json()
        except requests.exceptions.Timeout:
            print(f"超级鹰第{retry+1}次超时，重试...")
    return {"err_no": -1, "err_str": "连接超时"}


# ── 滑块验证 ─────────────────────────────────────────────────────────────────
async def do_slider(page):
    """模拟人工拖动滑块，用超级鹰识别缺口位置"""
    slider = page.locator('.ui-slider-btn')
    box = await slider.bounding_box()
    start_x = box['x'] + box['width'] / 2
    start_y = box['y'] + box['height'] / 2

    for attempt in range(3):
        await page.mouse.move(start_x, start_y)
        await page.mouse.down()
        await asyncio.sleep(1)

        await page.wait_for_selector('.jigimgB', state='visible', timeout=10000)
        bg_bytes = await page.locator('.jigimgB').screenshot()
        result = recognize_captcha(bg_bytes)

        if result.get('err_no') != 0:
            await page.mouse.up()
            await asyncio.sleep(1)
            continue

        # 将识别到的缺口坐标换算为拖动距离
        gap_x = max(int(p.split(',')[0]) for p in result['pic_str'].split('|'))
        img_w = pil_open(io.BytesIO(bg_bytes)).width
        wrap_box = await page.locator('.ui-slider-wrap').bounding_box()
        draggable = (wrap_box['width'] - box['width']) if wrap_box else 266
        target_x = int(gap_x / img_w * draggable)

        # 人性化拖动（随机步长 + 微抖动）
        current = 0
        while current < target_x:
            step = random.randint(3, 10)
            current = min(current + step, target_x)
            await page.mouse.move(start_x + current, start_y + random.uniform(-0.5, 0.5))
            await asyncio.sleep(random.uniform(0.02, 0.06))

        await asyncio.sleep(0.5)
        await page.mouse.up()
        await asyncio.sleep(2)

        # 验证是否通过
        for kw in ['验证成功', '成功', 'success']:
            if await page.locator(f'text={kw}').count() > 0:
                return True
        cls = await page.locator('.ui-slider-btn').get_attribute('class') or ''
        if 'init' not in cls:
            return True

    return False


async def login_erp(page):
    await page.goto(ERP_URL)
    await page.wait_for_load_state("networkidle")
    await page.fill('#_easyui_textbox_input3', USERNAME)
    await asyncio.sleep(1)
    await page.fill('#_easyui_textbox_input2', PASSWORD)
    await asyncio.sleep(1)
    if not await do_slider(page):
        return False
    await page.click('button.loginbtn')
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)
    return 'login' not in page.url


def get_frame_by_url(page, keyword):
    return next((f for f in page.frames if keyword in f.url), None)


async def navigate_to_asset(page):
    await page.click('text=集团资产管理')
    await asyncio.sleep(1)
    await page.locator('text=资产信息管理').first.click()
    await asyncio.sleep(1)
    await page.locator('text=资产信息管理').nth(1).click()
    await asyncio.sleep(2)
    await page.wait_for_load_state("networkidle")


async def search_device(page, device_code):
    frame = get_frame_by_url(page, ASSET_FRAME_URL)
    if not frame:
        return False, ''
    await frame.evaluate("v => $('#sbbh').textbox('setValue', v)", device_code)
    await asyncio.sleep(1)
    await frame.locator('a[onclick="searchFun()"]').first.click()
    await asyncio.sleep(2)
    await frame.wait_for_selector('.datagrid-btable tbody tr', timeout=10000)
    rows = frame.locator('.datagrid-btable tbody tr')
    for i in range(await rows.count()):
        row = rows.nth(i)
        if 'height: 1px' not in (await row.get_attribute('style') or ''):
            await row.click()
            await asyncio.sleep(1)
            break
    else:
        return False, ''
    # 点"修改"按钮
    await frame.evaluate("""
        () => {
            const btns = document.querySelectorAll('.datagrid-toolbar a.l-btn');
            for (const btn of btns) {
                if (btn.querySelector('.l-btn-text')?.textContent.trim() === '修改') { btn.click(); return; }
            }
        }
    """)
    await asyncio.sleep(2)
    return True, ''


async def modify_device(page, receiver_email, location):
    """在修改表单里填写接收人邮箱和位置"""
    modify_frame = None
    for _ in range(10):
        modify_frame = get_frame_by_url(page, MODIFY_FRAME_URL)
        if modify_frame: break
        await asyncio.sleep(1)
    if not modify_frame:
        return False

    await modify_frame.evaluate("v => $('#assetLocation').textbox('setValue', v)", location)
    await asyncio.sleep(1)
    await modify_frame.locator('#userId_button').click()
    await asyncio.sleep(3)

    select_frame = None
    for _ in range(10):
        select_frame = get_frame_by_url(page, SELECT_USER_FRAME_URL)
        if select_frame: break
        await asyncio.sleep(1)
    if not select_frame:
        return False

    await select_frame.evaluate("v => $('#userMail').textbox('setValue', v)", receiver_email)
    await asyncio.sleep(1)
    await select_frame.evaluate("() => initUserList()")
    await asyncio.sleep(2)
    await select_frame.wait_for_selector('.datagrid-btable tbody tr', timeout=10000)
    await select_frame.locator('.datagrid-btable tbody tr').first.click()
    await asyncio.sleep(1)
    await select_frame.evaluate("""
        () => {
            const btns = document.querySelectorAll('.datagrid-toolbar a.l-btn');
            for (const b of btns) {
                if (b.querySelector('.l-btn-text')?.textContent.trim() === '选定人员') { b.click(); return; }
            }
        }
    """)
    await asyncio.sleep(2)
    await modify_frame.evaluate("() => submitForm()")
    await asyncio.sleep(2)

    for target in [page, modify_frame]:
        try:
            await target.wait_for_selector('text=确定', timeout=5000)
            await target.locator('text=确定').click()
            return True
        except: pass
    return True


async def _operate_single_device(page, device_code, receiver_email, location):
    """在已登录的 page 上完成单台设备的资产变更"""
    # 每台设备前回主页，避免上一台提交后左侧菜单折叠/消失
    await page.goto(ERP_URL.split("?")[0].replace("/auth/login", "").replace("cpssso", "cps"))
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)
    await navigate_to_asset(page)

    found, _ = await search_device(page, device_code)
    if not found:
        return False, "设备编号未找到"

    result = await modify_device(page, receiver_email, location)
    return result, "" if result else "修改失败"


async def process_erp_tasks_batch(devices_info, instance_code):
    """一次登录，批量处理所有设备"""
    lock = acquire_erp_lock()
    if not lock:
        send_message(MY_CHAT_ID, f"❌ ERP锁等待超时\n审批实例：{instance_code}")
        return
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=["--no-proxy-server"])
            page = await (await browser.new_context(viewport={"width": 1920, "height": 1080})).new_page()

            if not await login_erp(page):
                await browser.close()
                send_message(MY_CHAT_ID, f"❌ ERP登录失败\n审批实例：{instance_code}")
                return

            total = len(devices_info)
            for i, dev in enumerate(devices_info, 1):
                ok, err = await _operate_single_device(page, dev['device_code'], dev['receiver_email'], dev['location'])
                if ok:
                    send_message(MY_CHAT_ID,
                        f"✅ [{i}/{total}] 资产变动完成\n设备：{dev['device_code']}\n接收人：{dev['receiver_email']}\n位置：{dev['location']}")
                else:
                    send_message(MY_CHAT_ID,
                        f"❌ [{i}/{total}] 失败，请人工处理\n设备：{dev['device_code']}\n原因：{err}")
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
    devices = parse_approval_form(form)
    if not devices:
        send_message(MY_CHAT_ID, f"⚠️ 审批单未找到设备明细：{instance_code}")
        return

    send_message(MY_CHAT_ID, f"🔧 开始处理审批 {instance_code}\n共 {len(devices)} 台设备")

    batch = []
    for dev in devices:
        email = dev['receiver_id'] if '@' in str(dev['receiver_id']) else get_user_email(dev['receiver_id'])
        if email:
            batch.append({**dev, 'receiver_email': email})

    if batch:
        asyncio.run(process_erp_tasks_batch(batch, instance_code))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: py erp_asset.py <instance_code>")
        sys.exit(2)
    instance_code = sys.argv[1]
    try:
        handle_approval(instance_code)
    except Exception as e:
        traceback.print_exc()
        send_message(MY_CHAT_ID, f"❌ ERP自动化异常\n{instance_code}\n{e}")
        sys.exit(1)
