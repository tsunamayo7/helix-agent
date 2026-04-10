"""$CMEM → Qdrant bridge — syncs important observations to vector search.

Runs as daemon (30min interval) or one-shot.
Syncs type=feature/bugfix/discovery observations to Qdrant mem0_shared.
Uses content_hash for deduplication.
Token cost: 0 (uses Ollama embedding locally).
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# --- Config ---
CMEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
QDRANT_URL = "http://localhost:6333"
QDRANT_MEMORY_URL = "http://localhost:8080"
OLLAMA_URL = "http://localhost:11434"
COLLECTION = "mem0_shared"
EMBED_MODEL = "qwen3-embedding:8b"
SYNC_TYPES = ("feature", "bugfix", "discovery")
STATE_FILE = Path.home() / ".helix-agent" / "cmem_bridge_state.json"
USER_ID = "tsunamayo7"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_sync_epoch": 0, "synced_hashes": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_new_observations(last_epoch: int) -> list[dict]:
    """Fetch important observations from $CMEM newer than last sync."""
    if not CMEM_DB.exists():
        return []
    conn = sqlite3.connect(str(CMEM_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in SYNC_TYPES)
    rows = conn.execute(f"""
        SELECT id, title, narrative, type, project, content_hash, created_at_epoch
        FROM observations
        WHERE type IN ({placeholders})
        AND created_at_epoch > ?
        ORDER BY created_at_epoch ASC
        LIMIT 50
    """, (*SYNC_TYPES, last_epoch)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def qdrant_has_hash(content_hash: str) -> bool:
    """Check if observation already exists in Qdrant by content_hash."""
    try:
        resp = httpx.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll", json={
            "filter": {
                "must": [{"key": "content_hash", "match": {"value": content_hash}}]
            },
            "limit": 1,
        }, timeout=5)
        data = resp.json()
        return len(data.get("result", {}).get("points", [])) > 0
    except Exception:
        return False


def embed_text(text: str) -> list[float]:
    """Generate embedding via Ollama."""
    resp = httpx.post(f"{OLLAMA_URL}/api/embed", json={
        "model": EMBED_MODEL,
        "input": text,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Ollama returns embeddings in "embeddings" array
    embeddings = data.get("embeddings", [])
    if embeddings:
        return embeddings[0]
    return data.get("embedding", [])


def upsert_to_qdrant(obs: dict, vector: list[float]):
    """Insert observation into Qdrant."""
    point_id = abs(hash(obs["content_hash"])) % (2**63)
    payload = {
        "memory": f"{obs['title']}: {obs['narrative'][:500]}",
        "user_id": USER_ID,
        "source": "cmem_bridge",
        "cmem_id": obs["id"],
        "type": obs["type"],
        "project": obs.get("project", ""),
        "content_hash": obs["content_hash"],
        "created_at": datetime.fromtimestamp(
            obs["created_at_epoch"] / 1000, tz=timezone.utc
        ).isoformat(),
    }
    httpx.put(f"{QDRANT_URL}/collections/{COLLECTION}/points", json={
        "points": [{
            "id": point_id,
            "vector": vector,
            "payload": payload,
        }]
    }, timeout=10).raise_for_status()


def sync():
    """Main sync logic."""
    state = load_state()
    observations = get_new_observations(state["last_sync_epoch"])

    if not observations:
        return 0

    synced = 0
    for obs in observations:
        content_hash = obs.get("content_hash", "")
        if not content_hash:
            continue
        if content_hash in state["synced_hashes"]:
            continue
        if qdrant_has_hash(content_hash):
            state["synced_hashes"].append(content_hash)
            continue

        try:
            text = f"{obs['title']} {obs['narrative']}"
            vector = embed_text(text[:2000])
            upsert_to_qdrant(obs, vector)
            state["synced_hashes"].append(content_hash)
            state["last_sync_epoch"] = max(state["last_sync_epoch"], obs["created_at_epoch"])
            synced += 1
        except Exception as e:
            print(f"[Bridge] Error syncing obs#{obs['id']}: {e}", file=sys.stderr)

    # Keep only last 500 hashes
    state["synced_hashes"] = state["synced_hashes"][-500:]
    save_state(state)
    return synced


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 1800
        print(f"[Bridge] Watching (interval: {interval}s)")
        while True:
            try:
                n = sync()
                if n:
                    print(f"[Bridge] Synced {n} observations")
            except Exception as e:
                print(f"[Bridge] Error: {e}", file=sys.stderr)
            time.sleep(interval)
    else:
        n = sync()
        print(f"Synced {n} observations" if n else "No new observations")
        # Heartbeat
        try:
            from supervisor import write_heartbeat
            write_heartbeat("cmem_bridge", {"synced": n})
        except ImportError:
            pass
