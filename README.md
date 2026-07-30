# 飞书 × ERP 智能助手 / Feishu ERP Agent

> 用飞书审批驱动 ERP 自动化，让设备管理从"人工操作"变成"审批即完成"。

---

## 项目背景

某企业 1000+ 人规模，设备采购/领用/调拨每天产生大量 ERP 录入工作。流程是：

1. 员工在飞书提交审批（入职领用 / 在职变更 / 离职归还）
2. 审批通过后，**行政人员手动登录 ERP 逐台录入** — 耗时、易出错
3. 本项目把步骤 2 完全自动化：审批通过瞬间，机器人自动完成 ERP 操作并飞书通知结果

---

## 核心能力

| 能力 | 说明 |
|------|------|
| 飞书审批监听 | 长连接接收审批回调，毫秒级触发 |
| ERP 浏览器自动化 | Playwright 操作内网 ERP，支持滑块验证码识别（超级鹰打码） |
| 并发控制 | TCP 端口锁，防止多个 ERP 任务同时抢 session |
| RAG 知识检索 | Ollama 本地向量化 Obsidian 笔记，混合检索增强回复 |
| 飞书消息推送 | 操作结果实时卡片通知 |

---

## 系统架构

```
飞书审批通过
    │
    ▼
listener.js ──────────────────────── 飞书长连接 (WebSocket)
    │  审批回调
    ▼
erp_asset.py / erp_device_borrow.py  ── Playwright ──► ERP 内网系统
    │  结果
    ▼
飞书卡片通知

旁路：
device_approval_poller.js  ─── 轮询多维表格 ──► 触发 erp_device_borrow.py
rag_query.py               ─── ChromaDB + Ollama ──► 知识检索增强回复
```

---

## 技术栈

- **飞书 SDK**：lark-oapi（Python）、lark-cli（Node.js）
- **浏览器自动化**：Playwright（async）
- **验证码识别**：超级鹰滑块打码平台 + PIL 图像处理
- **向量数据库**：ChromaDB（本地持久化）
- **嵌入模型**：nomic-embed-text（via Ollama，完全本地，不联网）
- **进程管理**：Node.js `child_process.spawn`（detached 模式，防父进程重启杀子进程）

---

## 关键技术细节

### 1. 滑块验证码自动识别

ERP 登录需要拖动滑块。本项目流程：

1. 鼠标按下，截图滑块背景（缺口图）
2. Base64 编码发给超级鹰识别 API → 返回缺口 x 坐标
3. 换算为拖动距离，模拟人工拖动（随机步长 + 微抖动）

```python
# 人性化拖动，避免被检测为机器人
current = 0
while current < target_x:
    step = random.randint(3, 10)
    current = min(current + step, target_x)
    await page.mouse.move(start_x + current, start_y + random.uniform(-0.5, 0.5))
    await asyncio.sleep(random.uniform(0.02, 0.06))
```

### 2. ERP 并发互斥锁

`erp_asset.py` 和 `erp_device_borrow.py` 共用同一个 ERP 账号。并发登录会导致 session 互踢。解决方案：**TCP 端口占位锁**——谁先绑定端口谁先跑，其余等待。

```python
def acquire_erp_lock(timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(('127.0.0.1', ERP_LOCK_PORT))
            s.listen(1)
            return s
        except OSError:
            s.close()
            time.sleep(5)
    return None
```

### 3. 子进程生命周期管理

listener.js 重启时，如果 ERP Python 进程是普通子进程，会被一起杀掉（同进程组）。解决：`detached: true + unref()`。

```javascript
const child = spawn('py', [workerPath, instanceCode], {
  cwd: PROJECT_ROOT, stdio: 'ignore', detached: true
});
child.unref();  // 父进程重启不影响子进程继续跑
```

### 4. 混合 RAG 检索

纯向量检索对中文专有名词效果差（如"展厅预约""资产变动"）。本项目用三路融合：

```python
# 综合分 = 向量相似度×0.5 + 文件名LCS×0.35 + 内容bigram命中率×0.15
final = round(vec_s * 0.5 + fname_s * 0.35 + kw_s * 0.15, 4)
```

文件名 LCS（最长公共中文子串）能精准命中专有名词文档，弥补向量检索的不足。

---

## 项目结构

```
feishu-erp-agent/
├── erp/
│   ├── erp_asset.py          # 资产变更自动化（飞书审批 → ERP 修改接收人/位置）
│   └── erp_device_borrow.py  # 设备领用/归还自动化
├── rag/
│   └── rag_query.py          # 混合检索查询脚本
├── bot/
│   └── device_erp_poller.js  # 多维表格轮询器（触发 ERP 操作）
├── .env.example              # 环境变量模板
├── .gitignore
└── README.md
```

---

## 快速开始

### 1. 安装依赖

```bash
# Python
pip install playwright lark-oapi chromadb requests pillow
playwright install chromium

# Node.js
npm install -g @larksuite/cli
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入真实值
```

### 3. 启动 RAG（可选）

```bash
# 安装 Ollama 并拉取嵌入模型
ollama pull nomic-embed-text

# 向量化知识库（替换为你的 Obsidian vault 路径）
VAULT_DIR=/path/to/vault py rag/vectorize_vault.py
```

### 4. 运行

```bash
# ERP 资产变更（由 listener.js 自动调用，也可手动测试）
py erp/erp_asset.py <审批实例ID>

# 多维表格轮询器
node bot/device_erp_poller.js
```

---

## 环境变量说明

| 变量名 | 说明 |
|--------|------|
| `FEISHU_APP_ID` | 飞书自建应用 App ID |
| `FEISHU_APP_SECRET` | 飞书自建应用 App Secret |
| `FEISHU_CHAT_ID` | 通知消息发送的群/个人 chat_id |
| `ERP_URL` | ERP 登录页 URL |
| `ERP_USERNAME` / `ERP_PASSWORD` | ERP 账号密码 |
| `CHAOJIYING_USER/PASS/SOFTID` | 超级鹰打码平台账号 |
| `APPROVAL_CODE_ASSET_CHANGE` | 飞书资产变动审批的 approval_code |
| `APPROVAL_CODE_DEVICE_BORROW` | 飞书设备领用/归还审批的 approval_code |
| `BASE_TOKEN` | 飞书多维表格 App Token |
| `TABLE_ID` | 多维表格 Table ID |

---

## 注意事项

- 本项目为**内网企业工具**，ERP 为特定系统，直接运行需要对应的内网环境
- `.env` 文件已在 `.gitignore` 中排除，**绝不能提交真实凭证**
- 超级鹰为第三方商业打码服务，使用需自行注册并遵守其服务条款

---

*本项目展示了飞书开放平台 + 企业内网系统自动化的完整实践，包含进程管理、验证码识别、向量检索等工程细节。*
