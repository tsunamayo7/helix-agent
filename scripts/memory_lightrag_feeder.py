"""memory/ → LightRAG feeder — syncs important memory files to knowledge graph.

Feeds feedback/user/reference type files to LightRAG for graph-based search.
Uses SHA-256 hash to avoid re-feeding unchanged files.
Token cost: 0 (LightRAG uses local Ollama for extraction).
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import httpx

# --- Config ---
MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
LIGHTRAG_URL = "http://localhost:9621"
STATE_FILE = Path.home() / ".helix-agent" / "lightrag_feed_state.json"
FEED_TYPES = {"feedback", "user", "reference"}
EXCLUDE_PATTERNS = {"_backup_*", "archive/*", "content/*", ".*", "MEMORY.md", "active_tasks.json", "session_checkpoint.json"}


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"file_hashes": {}, "last_feed": ""}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_frontmatter(filepath: Path) -> dict:
    """Extract frontmatter metadata from markdown file."""
    text = filepath.read_text(encoding="utf-8")
    match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not match:
        return {}
    meta = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta


def file_hash(filepath: Path) -> str:
    return hashlib.sha256(filepath.read_bytes()).hexdigest()[:16]


def is_excluded(filepath: Path) -> bool:
    name = filepath.name
    for pattern in EXCLUDE_PATTERNS:
        if pattern.startswith("*"):
            if name.endswith(pattern[1:]):
                return True
        elif pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif name == pattern:
            return True
    return False


def lightrag_insert(text: str, description: str = "") -> bool:
    """Insert text into LightRAG."""
    try:
        resp = httpx.post(f"{LIGHTRAG_URL}/documents/text", json={
            "text": text,
            "description": description,
        }, timeout=120)
        return resp.status_code in (200, 201)
    except Exception as e:
        print(f"[Feeder] LightRAG insert error: {e}", file=sys.stderr)
        return False


def lightrag_healthy() -> bool:
    try:
        resp = httpx.get(f"{LIGHTRAG_URL}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def feed():
    """Main feed logic."""
    if not lightrag_healthy():
        print("[Feeder] LightRAG not available, skipping")
        return 0

    state = load_state()
    fed = 0

    for md_file in sorted(MEMORY_DIR.glob("*.md")):
        if is_excluded(md_file):
            continue

        meta = parse_frontmatter(md_file)
        if meta.get("type") not in FEED_TYPES:
            continue

        current_hash = file_hash(md_file)
        if state["file_hashes"].get(md_file.name) == current_hash:
            continue

        content = md_file.read_text(encoding="utf-8")
        # Prepend file metadata for context
        header = f"Source: {md_file.name}\nType: {meta.get('type', 'unknown')}\nName: {meta.get('name', '')}\n\n"

        if lightrag_insert(header + content, description=meta.get("name", md_file.stem)):
            state["file_hashes"][md_file.name] = current_hash
            fed += 1
            print(f"[Feeder] Fed: {md_file.name}")

    state["last_feed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_state(state)
    return fed


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        state = load_state()
        print(f"Last feed: {state.get('last_feed', 'never')}")
        print(f"Files tracked: {len(state.get('file_hashes', {}))}")
        print(f"LightRAG: {'OK' if lightrag_healthy() else 'DOWN'}")
    else:
        n = feed()
        print(f"Fed {n} files" if n else "No changes to feed")
