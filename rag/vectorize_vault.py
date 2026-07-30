"""
vectorize_vault.py — Obsidian vault 增量向量化

用 nomic-embed-text（本地 Ollama）把所有 .md 文件向量化存入 ChromaDB。
支持增量更新：只处理新增/修改的文件，删除已移除的文件。

用法：
  py vectorize_vault.py           # 增量更新
  py vectorize_vault.py --full    # 强制全量重建

环境变量：
  VAULT_DIR   Obsidian vault 根目录（默认 ./vault）
  RAG_DB_DIR  ChromaDB 存储目录（默认脚本同级 chroma_db/）
"""

import os, sys, json, time, argparse, http.client
import chromadb

VAULT_DIR   = os.environ.get("VAULT_DIR",   os.path.join(os.path.dirname(__file__), "..", "vault"))
DB_DIR      = os.environ.get("RAG_DB_DIR",  os.path.join(os.path.dirname(__file__), "chroma_db"))
STATE_FILE  = os.path.join(DB_DIR, "vault_state.json")   # 记录每个文件的 mtime+size，实现增量
COLLECTION  = "vault_docs"
OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
EMBED_MODEL = "nomic-embed-text"
CHUNK_SIZE    = 1200   # 字符数，约 600 中文字
CHUNK_OVERLAP = 150

# nomic-embed-text 非对称 embedding 前缀（大幅提升召回质量）
DOC_PREFIX   = "search_document: "
QUERY_PREFIX = "search_query: "    # rag_query.py 侧对应

# 跳过系统/日志类目录（根据自己 vault 结构调整）
SKIP_DIRS = {
    "meta/claude_memory_backup",
    "meta/daily",
}

# ── 文本处理 ──────────────────────────────────────────────────────────────────

def strip_frontmatter(text: str) -> str:
    """去掉 YAML frontmatter（--- ... --- 块），只保留正文。"""
    t = text.strip()
    if not t.startswith("---"):
        return t
    end = t.find("\n---", 3)
    return t[end + 4:].strip() if end != -1 else t

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """按固定字符数切块，保留重叠以避免关键信息被截断。"""
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return chunks

# ── Embedding ─────────────────────────────────────────────────────────────────

def embed(texts: list[str]) -> list[list[float]]:
    """调用本地 Ollama 批量生成向量，带重试（Ollama 首次加载模型时会 502）。"""
    delays = [5, 10, 20, 30]
    for attempt, wait in enumerate(delays + [None]):
        try:
            body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
            conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=120)
            conn.request("POST", "/api/embed", body=body,
                         headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
            resp = conn.getresponse()
            status, data = resp.status, resp.read()
            conn.close()
            if status == 502:
                if wait is None:
                    raise RuntimeError("embed 多次重试均 502，Ollama 可能未就绪")
                print(f"\n  [502 模型加载中，等{wait}s...]", end="", flush=True)
                time.sleep(wait)
                continue
            if status != 200:
                raise RuntimeError(f"Ollama 返回 {status}")
            return json.loads(data)["embeddings"]
        except (http.client.HTTPException, OSError) as e:
            if wait is None:
                raise
            print(f"\n  [连接异常: {e}，等{wait}s...]", end="", flush=True)
            time.sleep(wait)
    raise RuntimeError("embed 重试均失败")

# ── 状态管理（实现增量更新）─────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def file_sig(path):
    """文件签名（mtime + size），快速判断是否变化，无需重算 hash。"""
    st = os.stat(path)
    return f"{st.st_mtime:.0f}:{st.st_size}"

# ── vault 扫描 ────────────────────────────────────────────────────────────────

def scan_vault():
    """遍历 vault，返回 {rel_path: abs_path}，跳过 SKIP_DIRS。"""
    result = {}
    for root, dirs, files in os.walk(VAULT_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if not f.endswith(".md"):
                continue
            abs_path = os.path.join(root, f)
            rel_path = os.path.relpath(abs_path, VAULT_DIR).replace("\\", "/")
            if any(rel_path.startswith(s + "/") or rel_path == s for s in SKIP_DIRS):
                continue
            result[rel_path] = abs_path
    return result

# ── ChromaDB 操作 ─────────────────────────────────────────────────────────────

def add_file(col, rel_path, abs_path):
    """读文件 → 切块 → 向量化 → upsert 进 ChromaDB。"""
    with open(abs_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    text = strip_frontmatter(raw).strip()
    if not text:
        return 0
    title  = os.path.splitext(os.path.basename(rel_path))[0]
    chunks = chunk_text(text)
    ids    = [f"{rel_path}::chunk{i}" for i in range(len(chunks))]
    metas  = [{"source": rel_path, "title": title, "chunk": i, "total": len(chunks)}
              for i in range(len(chunks))]
    vectors = embed([DOC_PREFIX + c for c in chunks])
    col.upsert(ids=ids, embeddings=vectors, documents=chunks, metadatas=metas)
    return len(chunks)

def delete_file(col, rel_path, state):
    """删除某文件在 ChromaDB 里的所有 chunk。"""
    n = state.get(rel_path, {}).get("chunks", 0)
    if n:
        try:
            col.delete(ids=[f"{rel_path}::chunk{i}" for i in range(n)])
        except Exception:
            pass

# ── 主逻辑 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="强制全量重建（清空旧数据）")
    args = parser.parse_args()

    os.makedirs(DB_DIR, exist_ok=True)
    client = chromadb.PersistentClient(path=DB_DIR)

    if args.full:
        print("⚠️  全量重建：清空旧数据...")
        try:
            client.delete_collection(COLLECTION)
        except Exception:
            pass

    col = client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})

    # 预热：触发模型加载，确保就绪后再处理正式文件
    print("⏳ 预热 nomic-embed-text（首次加载约需 10-30s）...", end=" ", flush=True)
    embed(["warmup"])
    print("✅ 就绪")

    state     = {} if args.full else load_state()
    vault     = scan_vault()
    new_state = {}
    added = updated = deleted = skipped = 0
    t0 = time.time()

    # 新增/更新
    total = len(vault)
    for idx, (rel, abs_path) in enumerate(vault.items(), 1):
        sig = file_sig(abs_path)
        old = state.get(rel, {})
        if not args.full and old.get("sig") == sig:
            skipped += 1
            new_state[rel] = old
            continue
        action = "更新" if rel in state else "新增"
        print(f"[{idx}/{total}] {action} {rel}", end=" ... ", flush=True)
        try:
            if rel in state:
                delete_file(col, rel, state)
            n = add_file(col, rel, abs_path)
            new_state[rel] = {"sig": sig, "chunks": n}
            print(f"{n} chunks ✅")
            added += (action == "新增")
            updated += (action == "更新")
        except Exception as e:
            print(f"❌ {e}")
            new_state[rel] = old

    # 删除已移除的文件
    for rel in state:
        if rel not in vault:
            print(f"删除 {rel}")
            delete_file(col, rel, state)
            deleted += 1

    save_state(new_state)
    print(f"\n✅ 完成！耗时 {time.time() - t0:.1f}s")
    print(f"   新增 {added} · 更新 {updated} · 删除 {deleted} · 跳过 {skipped}")
    print(f"   ChromaDB 总条目数: {col.count()}")

if __name__ == "__main__":
    main()
