'use strict';
/**
 * device_erp_poller.js
 * 轮询多维表格里的设备申请记录，发现审批通过后自动触发 ERP 操作。
 *
 * 数据流：
 *   飞书审批通过 → device_approval_poller.js 写入 device_instance_map.json →
 *   本脚本轮询 → 查询审批状态 → APPROVED → 读取多维表格字段 →
 *   调用 erp_device_borrow.py → 结果通知飞书
 *
 * 用法：node device_erp_poller.js
 */

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const net = require('net');

// 从环境变量读取（本地开发使用 dotenv 或手动 export）
const LARK_CLI   = process.env.LARK_CLI_PATH || 'lark-cli';
const BASE_TOKEN = process.env.BASE_TOKEN    || '';
const TABLE_ID   = process.env.TABLE_ID      || '';
const CHAT_ID    = process.env.FEISHU_CHAT_ID || '';

const POLL_MS   = 15000;
const LOCK_PORT = 47393;  // TCP 端口锁，防止多实例

const SCRIPT_DIR        = __dirname;
const DATA_DIR          = path.join(SCRIPT_DIR, '..', 'data');
const INSTANCE_MAP_FILE = path.join(DATA_DIR, 'device_instance_map.json');
const PROC_FILE         = path.join(DATA_DIR, 'device_erp_processed.json');
const LOG_FILE          = path.join(SCRIPT_DIR, '..', 'logs', 'device_erp.log');
const ERP_SCRIPT        = path.join(SCRIPT_DIR, '..', 'erp', 'erp_device_borrow.py');
const DISABLE_FLAG      = path.join(DATA_DIR, 'device_erp_poller.disabled');

if (fs.existsSync(DISABLE_FLAG)) {
  console.log('[INFO] ERP轮询器已禁用（disabled flag 存在），退出');
  process.exit(0);
}

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

// 单实例锁：监听本地端口，已有实例时直接退出
const lockServer = net.createServer();
lockServer.listen(LOCK_PORT, '127.0.0.1', () => { startPolling(); });
lockServer.on('error', () => {
  console.error(`[ABORT] 端口 ${LOCK_PORT} 已被占用，已有实例运行中，本进程退出`);
  process.exit(0);
});

// ── 工具函数 ──────────────────────────────────────────────────────────────────

function log(msg) {
  const d  = new Date();
  const ts = `${d.getFullYear()}/${d.getMonth()+1}/${d.getDate()} ` +
             `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
  const line = `[${ts}] ${msg}`;
  console.log(line);
  try {
    const dir = path.dirname(LOG_FILE);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(LOG_FILE, line + '\n', 'utf8');
  } catch {}
}

function loadJson(file, def) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch { return def; }
}

function saveJson(file, data) {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFileSync(file, JSON.stringify(data, null, 2), 'utf8');
}

/** 封装 lark-cli 调用，统一返回解析后的 JSON。 */
function cli(args) {
  const r = spawnSync(LARK_CLI, args, { encoding: 'utf8', windowsHide: true, timeout: 30000, cwd: SCRIPT_DIR });
  const raw = (r.stdout || '') + (r.stderr || '');
  try { return JSON.parse(raw); }
  catch { return { ok: false, error: { message: raw.slice(0, 300) } }; }
}

/** 从飞书字段值（字符串/数组/对象）里提取文本。 */
function getStr(val) {
  if (!val) return '';
  if (typeof val === 'string') return val;
  if (Array.isArray(val)) {
    if (val.length === 0) return '';
    const v = val[0];
    if (typeof v === 'string') return v;
    if (v && typeof v.text === 'string') return v.text;
  }
  return '';
}

function getApprovalStatus(instanceCode) {
  const res = cli(['approval', 'instances', 'get', '--instance-code', instanceCode, '--as', 'user']);
  if (res.ok) return res.data?.status || '';
  log(`[WARN] 查询审批状态失败 ${instanceCode}: ${JSON.stringify(res.error)}`);
  return '';
}

function getBitableRecord(recordId) {
  const res = cli([
    'api', 'GET',
    `/open-apis/bitable/v1/apps/${BASE_TOKEN}/tables/${TABLE_ID}/records/${recordId}`,
    '--as', 'user',
  ]);
  if (!res.ok) return null;
  return res.data?.record?.fields || null;
}

function notifyFeishu(msg) {
  const card = {
    schema: '2.0',
    body: { elements: [{ tag: 'markdown', content: msg }] },
    header: { title: { tag: 'plain_text', content: 'ERP自动化通知' }, template: 'red' },
  };
  cli(['im', '+messages-send', '--chat-id', CHAT_ID, '--as', 'bot',
    '--msg-type', 'interactive', '--content', JSON.stringify(card)]);
}

/** 调用 erp_device_borrow.py 执行 ERP 操作。 */
function runERP(instanceCode) {
  log(`触发 ERP: ${instanceCode}`);
  const r = spawnSync('py', [ERP_SCRIPT, instanceCode], {
    encoding: 'utf8',
    windowsHide: true,
    timeout: 600000,  // 10 分钟（登录+滑块验证较慢）
  });
  const out = (r.stdout || '') + (r.stderr || '');
  return { ok: r.status === 0, output: out };
}

// ── 主轮询 ────────────────────────────────────────────────────────────────────

function poll() {
  const instanceMap = loadJson(INSTANCE_MAP_FILE, {});
  const processed   = new Set(loadJson(PROC_FILE, []));

  const pending = Object.keys(instanceMap).filter(id => !processed.has(id));
  if (pending.length === 0) return;

  for (const recordId of pending) {
    const instanceCode = instanceMap[recordId];
    const status = getApprovalStatus(instanceCode);

    if (!status) continue;

    if (status === 'REJECTED' || status === 'CANCELED') {
      log(`审批已${status === 'REJECTED' ? '拒绝' : '取消'}，跳过: ${recordId}`);
      processed.add(recordId);
      saveJson(PROC_FILE, [...processed]);
      continue;
    }

    if (status !== 'APPROVED') continue;

    log(`审批通过，触发 ERP: recordId=${recordId} instance=${instanceCode}`);

    const result = runERP(instanceCode);

    if (result.ok) {
      log(`✓ ERP处理完成: ${recordId}`);
    } else {
      log(`✗ ERP处理失败: ${recordId}\n${result.output.slice(0, 500)}`);
      // 失败通知由 erp_device_borrow.py 里的 send_message 发出
      // 这里额外记录一行日志
    }

    processed.add(recordId);
    saveJson(PROC_FILE, [...processed]);
  }
}

function startPolling() {
  log('── ERP设备领用/归还轮询器启动 ──');
  poll();
  setInterval(poll, POLL_MS);
}
