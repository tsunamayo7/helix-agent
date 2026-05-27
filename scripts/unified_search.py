"""Unified search — queries all memory systems and merges results.

Searches: Qdrant (vector), LightRAG (graph), $CMEM (SQL).
Merges, deduplicates, and scores results.
Usage: python unified_search.py "query text"
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

# --- Config ---
QDRANT_URL = "http://localhost:6333"
QDRANT_MEMORY_URL = "http://localhost:8080"
LIGHTRAG_URL = "http://localhost:9621"
OLLAMA_URL = "http://localhost:11434"
CMEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
COLLECTION = "mem0_shared"
EMBED_MODEL = "qwen3-embedding:8b"
TOP_K = 10


def search_qdrant(query: str, top_k: int = TOP_K) -> list[dict]:
    """Vector search via Qdrant."""
    try:
        # Get embedding
        resp = httpx.post(f"{OLLAMA_URL}/api/embed", json={
            "model": EMBED_MODEL, "input": query
        }, timeout=15)
        embeddings = resp.json().get("embeddings", [])
        vector = embeddings[0] if embeddings else resp.json().get("embedding", [])
        if not vector:
            return []

        # Search — support both unnamed and named vector collections
        try:
            info = httpx.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=5).json()
            has_named = bool(info.get("result", {}).get("config", {}).get("params", {}).get("sparse_vectors"))
        except Exception:
            has_named = False
        vec_payload = {"name": "dense", "vector": vector} if has_named else vector
        resp = httpx.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search", json={
            "vector": vec_payload,
            "limit": top_k,
            "with_payload": True,
        }, timeout=10)
        results = []
        for point in resp.json().get("result", []):
            payload = point.get("payload", {})
            results.append({
                "source": "qdrant",
                "score": round(point.get("score", 0), 3),
                "title": payload.get("memory", "")[:100],
                "content": payload.get("memory", ""),
                "metadata": {
                    "type": payload.get("type", ""),
                    "project": payload.get("project", ""),
                    "content_hash": payload.get("content_hash", ""),
                    "source_origin": payload.get("source", ""),
                },
            })
        return results
    except Exception as e:
        return [{"source": "qdrant", "error": str(e)}]


def search_lightrag(query: str, top_k: int = TOP_K) -> list[dict]:
    """Graph + vector search via LightRAG."""
    try:
        resp = httpx.post(f"{LIGHTRAG_URL}/query", json={
            "query": query,
            "mode": "hybrid",  # mix of local + global
            "top_k": top_k,
        }, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        response_text = data.get("response", "")
        if not response_text:
            return []
        return [{
            "source": "lightrag",
            "score": 0.7,  # LightRAG doesn't return scores
            "title": response_text[:100],
            "content": response_text[:500],
            "metadata": {"mode": "hybrid"},
        }]
    except Exception as e:
        return [{"source": "lightrag", "error": str(e)}]


def search_cmem(query: str, top_k: int = TOP_K) -> list[dict]:
    """SQLite FTS search via $CMEM."""
    if not CMEM_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(CMEM_DB), timeout=5)
        conn.row_factory = sqlite3.Row
        # Simple LIKE search (FTS not guaranteed)
        keywords = query.split()
        conditions = " AND ".join(f"(title LIKE ? OR narrative LIKE ?)" for _ in keywords)
        params = []
        for kw in keywords:
            params.extend([f"%{kw}%", f"%{kw}%"])
        rows = conn.execute(f"""
            SELECT id, title, narrative, type, project, created_at_epoch
            FROM observations
            WHERE {conditions}
            ORDER BY created_at_epoch DESC
            LIMIT ?
        """, (*params, top_k)).fetchall()
        conn.close()
        results = []
        for row in rows:
            results.append({
                "source": "cmem",
                "score": 0.5,
                "title": row["title"],
                "content": row["narrative"][:300] if row["narrative"] else "",
                "metadata": {
                    "cmem_id": row["id"],
                    "type": row["type"],
                    "project": row["project"],
                },
            })
        return results
    except Exception as e:
        return [{"source": "cmem", "error": str(e)}]


def merge_and_deduplicate(results: list[dict]) -> list[dict]:
    """Merge results from all sources, deduplicate by content similarity."""
    seen_titles = set()
    merged = []
    for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
        if "error" in r:
            continue
        title_key = r.get("title", "")[:50].lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        merged.append(r)
    return merged[:TOP_K]


def search(query: str) -> dict:
    """Run unified search across all backends."""
    start = time.time()
    all_results = []
    searched = []

    # Parallel-ish search (sequential for simplicity, fast enough locally)
    qdrant_results = search_qdrant(query)
    all_results.extend(qdrant_results)
    searched.append("qdrant")

    lightrag_results = search_lightrag(query)
    all_results.extend(lightrag_results)
    searched.append("lightrag")

    cmem_results = search_cmem(query)
    all_results.extend(cmem_results)
    searched.append("cmem")

    merged = merge_and_deduplicate(all_results)
    duration = round((time.time() - start) * 1000)

    return {
        "query": query,
        "results": merged,
        "searched": searched,
        "total": len(merged),
        "duration_ms": duration,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python unified_search.py 'query'")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    result = search(query)

    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Query: {result['query']}")
        print(f"Searched: {', '.join(result['searched'])} ({result['duration_ms']}ms)")
        print(f"Results: {result['total']}")
        print()
        for i, r in enumerate(result["results"], 1):
            print(f"  [{i}] ({r['source']}, {r['score']}) {r['title']}")
            if r.get("content"):
                print(f"      {r['content'][:150]}...")
            print()
