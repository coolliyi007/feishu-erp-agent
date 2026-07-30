"""
rag_query.py — 混合检索（向量 + 文件名匹配 + 内容关键词）

架构：
    Ollama nomic-embed-text 生成查询向量 →
    ChromaDB 向量检索（Top-15）+
    全量文件名 LCS 扫描 →
    综合评分 (vec×0.5 + fname×0.35 + bigram×0.15) →
    每文件保留最高分 chunk → 返回 Top-K

用法: py rag_query.py "查询文本" [top_k=3]
输出: JSON 数组 [{source, title, text, score}]，失败输出 []
"""
import sys, io, json, http.client, re, time, os
import chromadb

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

OLLAMA_HOST  = os.environ.get("OLLAMA_HOST", "127.0.0.1")
OLLAMA_PORT  = int(os.environ.get("OLLAMA_PORT", "11434"))
OLLAMA_PATH  = "/api/embed"
EMBED_MODEL  = "nomic-embed-text"
# 默认在脚本同级的 chroma_db 目录；也可通过环境变量覆盖
DB_DIR       = os.environ.get("RAG_DB_DIR", os.path.join(os.path.dirname(__file__), "chroma_db"))
COLLECTION   = "vault_docs"
QUERY_PREFIX = "search_query: "

# 系统/索引文件，不作为 RAG 上下文
SKIP_SOURCES = {"log.md", "index.md", "CLAUDE.md", "MEMORY.md"}


def embed_one(text: str) -> list[float]:
    body = json.dumps({"model": EMBED_MODEL, "input": text, "keep_alive": "10h"}).encode()
    conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=30)
    conn.request("POST", OLLAMA_PATH, body=body,
                 headers={"Content-Type": "application/json",
                          "Content-Length": str(len(body))})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if resp.status != 200:
        raise RuntimeError(f"Ollama {resp.status}")
    return json.loads(data)["embeddings"][0]


def longest_common_substring(a: str, b: str) -> int:
    """返回最长公共中文子串长度（滚动 DP，O(m×n) 时间，O(n) 空间）。"""
    a = re.sub(r'[^一-鿿]', '', a)
    b = re.sub(r'[^一-鿿]', '', b)
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    longest = 0
    dp = [[0] * (n + 1) for _ in range(2)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i-1] == b[j-1]:
                dp[i%2][j] = dp[(i-1)%2][j-1] + 1
                longest = max(longest, dp[i%2][j])
            else:
                dp[i%2][j] = 0
    return longest


def filename_score(query: str, source: str) -> float:
    """文件名与查询的最长公共中文子串 → 0~1 分。>= 4字算满分。"""
    fname = source.rsplit("/", 1)[-1].replace(".md", "")
    lcs = longest_common_substring(query, fname)
    if lcs >= 4:
        return min(1.0, lcs / 4)
    elif lcs >= 2:
        return lcs * 0.15
    return 0.0


def content_keyword_score(query: str, doc_text: str) -> float:
    """查询 bigram 在文档内容中的命中率。"""
    chinese = re.sub(r'[^一-鿿]', '', query)
    if len(chinese) < 2:
        return 0.0
    bigrams = {chinese[i:i+2] for i in range(len(chinese) - 1)}
    if not bigrams:
        return 0.0
    hits = sum(1 for g in bigrams if g in doc_text)
    return hits / len(bigrams)


def is_short_content(text: str) -> bool:
    """极短内容（MOC/hub 特征）：去除标点后 < 50 字。"""
    return len(re.sub(r'[^一-鿿\w]', '', text)) < 50


def main():
    t0 = time.time()
    query = sys.argv[1] if len(sys.argv) > 1 else ""
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    if not query.strip():
        print("[]")
        return

    try:
        client = chromadb.PersistentClient(path=DB_DIR)
        col = client.get_collection(COLLECTION)
    except Exception:
        print("[]")
        return

    total = col.count()
    if total == 0:
        print("[]")
        return

    # ── 向量检索 ─────────────────────────────────────────────────────────────
    try:
        vec = embed_one(QUERY_PREFIX + query)
        n_vec = min(15, total)
        vec_results = col.query(query_embeddings=[vec], n_results=n_vec)
        vec_map = {}
        for i in range(len(vec_results["ids"][0])):
            cid = vec_results["ids"][0][i]
            dist = vec_results["distances"][0][i]
            vec_map[cid] = {
                "vec_score": round(1 - dist, 4),
                "doc":  vec_results["documents"][0][i],
                "meta": vec_results["metadatas"][0][i],
            }
    except Exception:
        vec_map = {}

    # ── 全量扫描：文件名匹配 ─────────────────────────────────────────────────
    all_data = col.get(include=["documents", "metadatas"])
    fname_map = {}
    for i, cid in enumerate(all_data["ids"]):
        meta = all_data["metadatas"][i]
        src  = meta.get("source", "")
        if src.rsplit("/", 1)[-1] in SKIP_SOURCES or src in SKIP_SOURCES:
            continue
        fs = filename_score(query, src)
        if fs >= 0.3:
            fname_map[cid] = (fs, all_data["documents"][i], meta)

    # ── 合并计算综合分 ────────────────────────────────────────────────────────
    merged = {}
    for cid in set(vec_map) | set(fname_map):
        v    = vec_map.get(cid)
        f    = fname_map.get(cid)
        doc  = (v["doc"]  if v else f[1])
        meta = (v["meta"] if v else f[2])
        src  = meta.get("source", "")

        if src.rsplit("/", 1)[-1] in SKIP_SOURCES or src in SKIP_SOURCES:
            continue

        vec_s   = v["vec_score"] if v else 0.0
        fname_s = f[0] if f else 0.0
        kw_s    = content_keyword_score(query, doc)

        # 跳过极短 hub 文档（无文件名/关键词命中时）
        if is_short_content(doc) and fname_s < 0.3 and kw_s < 0.3:
            continue

        final = round(vec_s * 0.5 + fname_s * 0.35 + kw_s * 0.15, 4)
        merged[cid] = {
            "source":    src,
            "title":     meta.get("title", ""),
            "text":      doc[:500],
            "score":     final,
            "vec_score": vec_s,
            "fname_score": fname_s,
        }

    # 每个文件只保留最高分 chunk
    best = {}
    for info in merged.values():
        src = info["source"]
        if src not in best or info["score"] > best[src]["score"]:
            best[src] = info

    ranked = sorted(best.values(), key=lambda x: x["score"], reverse=True)
    output = [{"source": c["source"], "title": c["title"],
               "text": c["text"], "score": c["score"]}
              for c in ranked[:top_k] if c["score"] > 0.1]

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
