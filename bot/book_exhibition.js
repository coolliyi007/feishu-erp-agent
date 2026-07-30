/**
 * book_exhibition.js — 发起「展厅预约」飞书审批
 *
 * 用法：
 *   node book_exhibition.js \
 *     --customer "客户名称" \
 *     --reason   "参观目的" \
 *     --time     "2026-07-05 14:00" \
 *     --booker-open-id ou_xxx   (或 --booker-id user_id)
 *     [--dry-run]               (只打印请求体，不实际发起)
 *
 * 设计亮点：
 *   先发起审批 → 立刻回查实例是否真的存在（防止飞书 API 假成功）→
 *   核实通过后才触发通知，避免"发起成功"消息但审批系统里根本没有这条记录。
 */
'use strict';
const https = require('https');

// 从环境变量读取（配合 .env + dotenv 使用，或直接 export）
const APP_ID        = process.env.FEISHU_APP_ID     || '';
const APP_SECRET    = process.env.FEISHU_APP_SECRET  || '';
const APPROVAL_CODE = process.env.APPROVAL_CODE_EXHIBITION || '';

// 审批表单控件 ID（在飞书审批管理后台查看）
const W = {
  customer: process.env.W_CUSTOMER || 'widget_customer_id',
  reason:   process.env.W_REASON   || 'widget_reason_id',
  booker:   process.env.W_BOOKER   || 'widget_booker_id',
  dept:     process.env.W_DEPT     || 'widget_dept_id',
  time:     process.env.W_TIME     || 'widget_time_id',
};

// ── 参数解析 ───────────────────────────────────────────────────────────────────

function arg(name) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : null;
}

const dryRun       = process.argv.includes('--dry-run');
const customer     = arg('customer');
const reason       = arg('reason');
const time         = arg('time');
const bookerUserId = arg('booker-id');
const bookerOpenId = arg('booker-open-id');

if (!customer || !reason || !time || (!bookerUserId && !bookerOpenId)) {
  console.error('缺参数。需要 --customer --reason --time "YYYY-MM-DD HH:mm" 和 --booker-open-id（或 --booker-id）');
  process.exit(1);
}

// ── 工具函数 ──────────────────────────────────────────────────────────────────

/** "2026-07-05 14:00" → "2026-07-05T14:00:00+08:00" */
function toISO(s) {
  s = s.trim();
  if (/T/.test(s) && /[+Z]/.test(s)) return s;
  const t = s.replace(' ', 'T');
  return (t.length === 16 ? t + ':00' : t) + '+08:00';
}

function post(path, token, body) {
  return new Promise((res, rej) => {
    const payload = JSON.stringify(body);
    const req = https.request({
      hostname: 'open.feishu.cn', path, method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json; charset=utf-8',
                 'Content-Length': Buffer.byteLength(payload) },
    }, x => { let d = ''; x.on('data', c => d += c); x.on('end', () => { try { res(JSON.parse(d)); } catch { res({ raw: d }); } }); });
    req.on('error', rej); req.write(payload); req.end();
  });
}

function get(path, token) {
  return new Promise((resolve) => {
    const req = https.request({
      hostname: 'open.feishu.cn', path, method: 'GET',
      headers: { Authorization: `Bearer ${token}` },
    }, res => { let d = ''; res.on('data', c => d += c); res.on('end', () => { try { resolve(JSON.parse(d)); } catch { resolve(null); } }); });
    req.on('error', () => resolve(null)); req.end();
  });
}

async function getTenantToken() {
  const r = await post('/open-apis/auth/v3/tenant_access_token/internal', '',
    { app_id: APP_ID, app_secret: APP_SECRET });
  return r.tenant_access_token || '';
}

async function getUserIdByOpenId(token, openId) {
  const r = await get(`/open-apis/contact/v3/users/${openId}?user_id_type=open_id`, token);
  return r?.data?.user?.user_id || null;
}

async function getUserDepartment(token, openId) {
  const r = await get(
    `/open-apis/contact/v3/users/${openId}?user_id_type=open_id&department_id_type=open_department_id`,
    token,
  );
  const ids = r?.data?.user?.department_ids;
  return ids?.[0] || null;
}

/** 回查实例是否真的存在（防止 API 假成功） */
async function getInstance(token, instanceCode) {
  return await get(`/open-apis/approval/v4/instances/${instanceCode}`, token);
}

// ── 主流程 ────────────────────────────────────────────────────────────────────

(async () => {
  const token = await getTenantToken();
  if (!token) { console.error('❌ 获取 tenant_access_token 失败'); process.exit(1); }

  let userId = bookerUserId;
  if (!userId && bookerOpenId) userId = await getUserIdByOpenId(token, bookerOpenId);
  if (!userId) { console.error('❌ 无法确定预约人 user_id'); process.exit(1); }

  // 自动读预约人部门（需 contact:user.department:readonly 权限）
  const deptOd = bookerOpenId ? await getUserDepartment(token, bookerOpenId) : null;

  const form = [
    { id: W.customer, type: 'input',   value: customer },
    { id: W.reason,   type: 'input',   value: reason },
    { id: W.booker,   type: 'contact', value: [userId] },
    ...(deptOd ? [{ id: W.dept, type: 'department', value: [{ open_id: deptOd }] }] : []),
    { id: W.time,     type: 'date',    value: toISO(time) },
  ];

  const body = {
    approval_code: APPROVAL_CODE,
    user_id: userId,
    form: JSON.stringify(form),
  };

  if (dryRun) {
    console.log('=== DRY RUN ===\n', JSON.stringify(body, null, 2));
    return;
  }

  const r = await post('/open-apis/approval/v4/instances?user_id_type=user_id', token, body);

  if (r.code === 0 && r.data?.instance_code) {
    const inst = r.data.instance_code;

    // 关键：先核实，再通知（避免 API 返回 code=0 但审批实际未创建）
    const check = await getInstance(token, inst);
    const confirmed = check?.data?.instance_code === inst;

    if (confirmed) {
      console.log('✅ 审批已核实发起成功 instance_code:', inst);
      // 后续通知逻辑由调用方（listener.js）或 exhibition_notify.js 处理
      process.stdout.write(JSON.stringify({ ok: true, instance_code: inst }) + '\n');
    } else {
      console.error('❌ 发起接口返回成功但回查不到实例，视为失败');
      process.exit(1);
    }
  } else {
    console.error('❌ 发起失败 code=' + r.code + ' msg=' + r.msg);
    process.exit(1);
  }
})();
