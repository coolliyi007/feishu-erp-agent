"""
intent_classify.py — 用本地 LLM（qwen3:8b via Ollama）对飞书消息做意图分类

用法: py intent_classify.py "消息文本"
输出: JSON {"intent": "...", "hint": "..."}，失败时输出 {"intent": "chat", "hint": ""}

意图类别：
  flow_query     — 询问流程/SOP/操作步骤（报销/审批/申请怎么填）
  person_query   — 查询某人信息/身份/职责
  approval_query — 查询/发起审批，或问审批状态
  schedule_query — 查日程/会议/日历安排
  action_request — 要求执行某个操作（发消息/预约/修改数据）
  chat           — 闲聊/问候/模糊意图

设计思路：
  - 用本地 qwen3:8b，无需联网、零 API 费用、延迟 < 1s
  - think=false + num_predict=60 限制输出 token，保证速度
  - temperature=0 保证分类稳定可复现
  - 失败时 fallback 到 "chat"，不阻塞主流程
"""
import sys, json, http.client, re, time

OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
MODEL       = "qwen3:8b"

SYSTEM = """你是消息意图分类器。根据用户消息，输出JSON：{"intent":"<类别>","hint":"<一句话说明>"}

类别只能是以下6种之一：
- flow_query：问流程/SOP/操作步骤（如"报销怎么填""怎么申请""哪里提单"）
- person_query：问某人信息/身份/职责（如"XXX是谁""XXX管什么"）
- approval_query：查审批/发起审批/问审批状态（如"有什么待审批""帮我查审批"）
- schedule_query：查日程/会议/日历（如"今天有什么会""安排个会议"）
- action_request：要执行某操作（如"帮我发消息""预约展厅""修改XXX"）
- chat：其他/闲聊/问候/不明确

只输出JSON，不要任何其他内容，不要markdown代码块。"""


def classify(text: str) -> dict:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user",   "content": text[:200]},  # 截断避免过长输入
        ],
        "stream": False,
        "think": False,          # 关闭 qwen3 思维链，降低延迟
        "keep_alive": "10h",
        "options": {"num_predict": 60, "temperature": 0},
    }).encode()

    try:
        conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=20)
        conn.request("POST", "/api/chat", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
    except Exception:
        return {"intent": "chat", "hint": ""}

    if resp.status != 200:
        return {"intent": "chat", "hint": ""}

    content = json.loads(data).get("message", {}).get("content", "")
    m = re.search(r'\{[^}]+\}', content)
    return json.loads(m.group()) if m else {"intent": "chat", "hint": content[:50]}


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else ""
    if not text.strip():
        print('{"intent":"chat","hint":""}')
        return

    result = classify(text)

    # 确保 intent 合法，非法值 fallback 到 chat
    valid = {"flow_query", "person_query", "approval_query", "schedule_query", "action_request", "chat"}
    if result.get("intent") not in valid:
        result["intent"] = "chat"

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
