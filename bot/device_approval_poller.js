'use strict';
/**
 * device_approval_poller.js
 * 轮询飞书多维表格"领用与归还设备"表单，发现新提交后自动发起飞书审批。
 *
 * 数据流：
 *   员工在多维表格填写申请 →
 *   本脚本轮询（每10s）发现新记录 →
 *   调用飞书审批 API 发起审批 →
 *   将 instance_code 回写到多维表格"申请编号"列 →
 *   写入 device_instance_map.json 供 device_erp_poller.js 消费
 *
 * 用法：node device_approval_poller.js
 */

const { spawnSync } = require('child_process');
const fs   = require('fs');
const path = require('path');
const net  = require('net');

// 从环境变量读取
const LARK_CLI      = process.env.LARK_CLI_PATH  || 'lark-cli';
const BASE_TOKEN    = process.env.BASE_TOKEN      || '';
const TABLE_ID      = process.env.TABLE_ID        || '';
const APPROVAL_CODE = process.env.APPROVAL_CODE_DEVICE_BORROW || '';

// 归还设备时接收人固定为库管（通过环境变量配置，不要写死个人 ID）
const WAREHOUSE_OPEN_ID = process.env.WAREHOUSE_KEEPER_OPEN_ID || '';
const WAREHOUSE_EMAIL   = process.env.WAREHOUSE_KEEPER_EMAIL   || '';

const POLL_MS   = 10000;
const LOCK_PORT = 47392;  // TCP 单实例锁

const SCRIPT_DIR        = __dirname;
const DATA_DIR          = path.join(SCRIPT_DIR, '..', 'data');
const PROC_FILE         = path.join(DATA_DIR, 'device_approval_processed.json');
const INSTANCE_MAP_FILE = path.join(DATA_DIR, 'device_instance_map.json');
const LOG_FILE          = path.join(SCRIPT_DIR, '..', 'logs', 'device_approval.log');
const DISABLE_FLAG      = path.join(DATA_DIR, 'device_approval_poller.disabled');
const TMP_DATA          = path.join(SCRIPT_DIR, '_tmp_approval_data.json');
const TMP_WB            = path.join(SCRIPT_DIR, '_tmp_writeback.json');

// 审批表单控件 ID（在飞书审批管理后台查看）
const W = {
  SHIYOU:        process.env.W_SHIYOU        || 'widget_shiyou_id',
  SHIXIANG:      process.env.W_SHIXIANG      || 'widget_shixiang_id',
  TIJIAOREN:     process.env.W_TIJIAOREN     || 'widget_tijiaoren_id',
  MINGXI:        process.env.W_MINGXI        || 'widget_mingxi_id',
  SHEBEIBUHAO:   process.env.W_SHEBEIBUHAO   || 'widget_shebeibuhao_id',
  BANGONGDATING: process.env.W_BANGONGDATING || 'widget_bangongdating_id',
};

// 审批单选项 ID（事由/事项的 radioV2 选项值）
const SHIYOU_MAP = {
  '入职': process.env.RADIO_RUIZHI  || '',
  '在职': process.env.RADIO_ZAIZHI  || '',
  '离职': process.env.RADIO_LIZHI   || '',
};
const SHIXIANG_MAP = {
  '领用设备': process.env.RADIO_LINGYONG || '',
  '归还设备': process.env.RADIO_GUIHUAN  || '',
};

if (fs.existsSync(DISABLE_FLAG)) {
  console.log('[INFO] 轮询器已禁用（disabled flag 存在），退出');
  process.exit(0);
}

if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

// 单实例锁：绑定本地端口（原子操作，无竞态条件）
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
    if (!fs.existsSync(path.dirname(LOG_FILE))) fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, line + '\n', 'utf8');
  } catch {}
}

function loadProcessed() {
  try { return new Set(JSON.parse(fs.readFileSync(PROC_FILE, 'utf8'))); }
  catch { return new Set(); }
}

function saveProcessed(set) {
  fs.writeFileSync(PROC_FILE, JSON.stringify([...set], null, 2), 'utf8');
}

function loadInstanceMap() {
  try { return JSON.parse(fs.readFileSync(INSTANCE_MAP_FILE, 'utf8')); }
  catch { return {}; }
}

function saveInstanceMap(map) {
  fs.writeFileSync(INSTANCE_MAP_FILE, JSON.stringify(map, null, 2), 'utf8');
}

function cli(args, cwd) {
  const r = spawnSync(LARK_CLI, args, { encoding: 'utf8', windowsHide: true, timeout: 30000, cwd: cwd || SCRIPT_DIR });
  const raw = (r.stdout || '') + (r.stderr || '');
  try { return JSON.parse(raw); }
  catch { return { ok: false, error: { message: raw.slice(0, 300) } }; }
}

function getStr(val) {
  if (!val) return '';
  if (typeof val === 'string') return val;
  if (Array.isArray(val)) {
    const v = val[0];
    if (typeof v === 'string') return v;
    if (v?.text) return v.text;
  }
  return '';
}

// open_id 转换缓存（减少重复 API 调用）
const USER_ID_CACHE    = {};
const USER_EMAIL_CACHE = {};

function openIdToEmail(openId) {
  if (!openId) return '';
  if (USER_EMAIL_CACHE[openId]) return USER_EMAIL_CACHE[openId];
  const res = cli(['api', 'GET', `/open-apis/contact/v3/users/${openId}`,
    '--params', '{"user_id_type":"open_id"}', '--as', 'user']);
  if (res.ok) {
    const u = res.data?.user;
    const email = u?.enterprise_email || u?.email || '';
    if (email) USER_EMAIL_CACHE[openId] = email;
    return email;
  }
  return '';
}

function openIdToUserId(openId) {
  if (!openId) return '';
  if (USER_ID_CACHE[openId]) return USER_ID_CACHE[openId];
  const res = cli(['api', 'GET', `/open-apis/contact/v3/users/${openId}`,
    '--params', '{"user_id_type":"open_id"}', '--as', 'bot']);
  if (res.ok && res.data?.user?.user_id) {
    USER_ID_CACHE[openId] = res.data.user.user_id;
    return res.data.user.user_id;
  }
  return '';
}

// ── 多维表格查询 ──────────────────────────────────────────────────────────────

function listAllRecords() {
  const records = [];
  let offset = 0;
  for (;;) {
    const res = cli([
      'base', '+record-list',
      '--base-token', BASE_TOKEN, '--table-id', TABLE_ID,
      '--as', 'user', '--format', 'json', '--limit', '100', '--offset', String(offset),
    ]);
    if (!res.ok) { log(`[WARN] 拉取记录失败 offset=${offset}: ${JSON.stringify(res.error)}`); break; }

    const { data: rows, record_id_list: ids, fields: fieldNames, has_more } = res.data;
    const nameIdx = {};
    if (fieldNames) fieldNames.forEach((n, i) => { nameIdx[n] = i; });
    (ids || []).forEach((id, i) => records.push({ id, row: rows[i], nameIdx }));
    if (!has_more) break;
    offset += 100;
  }
  return records;
}

// ── 发起审批 ──────────────────────────────────────────────────────────────────

function createApproval(rec) {
  const { row, nameIdx } = rec;
  const shiyou   = getStr(row[nameIdx['事由']]);
  const shixiang = shiyou === '入职' ? getStr(row[nameIdx['事项-入职']])
                 : shiyou === '在职' ? getStr(row[nameIdx['事项-在职']])
                 : shiyou === '离职' ? getStr(row[nameIdx['事项-离职']])
                 : '';
  const shebeibuhao   = getStr(row[nameIdx['设备编号']]);
  const bangongdating = getStr(row[nameIdx['办公大厅']]) || (shixiang === '归还设备' ? '库房' : '');
  const tijiaoren_openid = row[nameIdx['提交人']]?.[0]?.id || '';
  const tijiaoren_uid    = openIdToUserId(tijiaoren_openid);

  const formFields = [
    { id: W.SHIYOU,   type: 'radioV2', value: SHIYOU_MAP[shiyou] || '' },
    { id: W.SHIXIANG, type: 'radioV2', value: SHIXIANG_MAP[shixiang] || '' },
    {
      id: W.MINGXI, type: 'fieldList',
      value: [[
        { id: W.SHEBEIBUHAO,   type: 'input', value: shebeibuhao },
        { id: W.BANGONGDATING, type: 'input', value: bangongdating },
      ]],
    },
  ];
  if (tijiaoren_uid) {
    formFields.splice(2, 0, { id: W.TIJIAOREN, type: 'contact', value: [tijiaoren_uid] });
  }

  const dataObj = {
    approval_code: APPROVAL_CODE,
    form: JSON.stringify(formFields),
    user_id: tijiaoren_uid || '',
  };

  fs.writeFileSync(TMP_DATA, JSON.stringify(dataObj), 'utf8');
  const res = cli(['api', 'POST', '/open-apis/approval/v4/instances',
    '--data', '@_tmp_approval_data.json', '--as', 'bot']);
  try { fs.unlinkSync(TMP_DATA); } catch {}
  return res;
}

// ── 回写申请编号 ──────────────────────────────────────────────────────────────

function getSerialNumber(instanceCode) {
  const res = cli(['approval', 'instances', 'get', '--instance-code', instanceCode, '--as', 'user']);
  return res.ok ? (res.data?.serial_number || instanceCode) : instanceCode;
}

function writeBack(recordId, instanceCode, instanceLink, bangongdating, submitterOpenId) {
  const serialNum = getSerialNumber(instanceCode);
  const fields = { '申请编号': { text: serialNum, link: instanceLink } };

  if (bangongdating === '库房') {
    // 归还设备：接收人固定为仓库管理员
    fields['办公大厅'] = '库房';
    if (WAREHOUSE_OPEN_ID) fields['接收人'] = [{ id: WAREHOUSE_OPEN_ID }];
  } else {
    // 领用设备：接收人 = 提交人
    if (submitterOpenId) fields['接收人'] = [{ id: submitterOpenId }];
  }

  fs.writeFileSync(TMP_WB, JSON.stringify({ fields }), 'utf8');
  const res = cli([
    'api', 'PUT', `/open-apis/bitable/v1/apps/${BASE_TOKEN}/tables/${TABLE_ID}/records/${recordId}`,
    '--data', '@_tmp_writeback.json', '--as', 'user',
  ]);
  try { fs.unlinkSync(TMP_WB); } catch {}
  return res;
}

// ── 主轮询 ────────────────────────────────────────────────────────────────────

function poll() {
  const processed  = loadProcessed();
  const allRecords = listAllRecords();
  const newOnes    = allRecords.filter(r => !processed.has(r.id));
  if (newOnes.length === 0) return;

  log(`发现 ${newOnes.length} 条新记录，开始发起审批`);

  for (const rec of newOnes) {
    const { row, nameIdx } = rec;
    const shiyou   = getStr(row[nameIdx['事由']]);
    const shixiang = shiyou === '入职' ? getStr(row[nameIdx['事项-入职']])
                   : shiyou === '在职' ? getStr(row[nameIdx['事项-在职']])
                   : shiyou === '离职' ? getStr(row[nameIdx['事项-离职']])
                   : '未知';
    const name = row[nameIdx['提交人']]?.[0]?.name || '未知';
    log(`处理 ${rec.id} — ${name} · ${shixiang}`);

    // 必填字段检查
    const sbh = getStr(row[nameIdx['设备编号']]);
    const bdt = getStr(row[nameIdx['办公大厅']]) || (shixiang === '归还设备' ? '库房' : '');
    if (!shiyou || !shixiang || !sbh || !bdt) {
      log(`[SKIP] 必填字段缺失 (事由=${shiyou||'空'} 事项=${shixiang||'空'} 设备=${sbh||'空'} 位置=${bdt||'空'})`);
      processed.add(rec.id);
      saveProcessed(processed);
      continue;
    }

    const tijiaoren_openid = row[nameIdx['提交人']]?.[0]?.id || '';
    const res = createApproval(rec);

    if (res.ok) {
      const code = res.data?.instance_code || '';
      const link = res.data?.instance_link || '';
      log(`✓ 审批已发起 ${code}  ${link}`);

      // 写入 instance_map，供 device_erp_poller.js 监听审批结果后触发 ERP
      const imap = loadInstanceMap();
      imap[rec.id] = code;
      saveInstanceMap(imap);

      const wb = writeBack(rec.id, code, link, bdt, tijiaoren_openid);
      log(wb.ok ? `  └ 申请编号、接收人已回写` : `  └ [WARN] 回写失败: ${JSON.stringify(wb.error)}`);
    } else {
      log(`✗ 发起失败 ${rec.id}: ${JSON.stringify(res.error)}`);
      // 失败不标记已处理，下次轮询重试
      continue;
    }

    processed.add(rec.id);
    saveProcessed(processed);
  }
}

function startPolling() {
  log('── 设备领用/归还审批轮询器启动 ──');
  poll();
  setInterval(poll, POLL_MS);
}
