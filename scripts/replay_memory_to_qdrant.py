#!/usr/bin/env python3
"""Memory ファイル → Qdrant 再注入スクリプト.

Mac移行後に失われた ~20セッション分のナレッジを Qdrant に再注入する。
既存データは維持し、追加のみ行う (知識の累積成長方針)。

Usage:
    python3 scripts/replay_memory_to_qdrant.py                    # dry-run
    python3 scripts/replay_memory_to_qdrant.py --execute           # 実行
    python3 scripts/replay_memory_to_qdrant.py --execute --dept    # dept_* にも投入
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

MEMORY_DIR = Path.home() / ".claude" / "projects" / "Development" / "memory"
QDRANT_URL = os.environ.get("QDRANT_URL", os.environ.get("QDRANT_URL", "http://localhost:6333"))
OLLAMA_HOST = os.environ.get("HELIX_OLLAMA_HOST", os.environ.get("HELIX_OLLAMA_HOST", "http://localhost:11434"))

DEPT_MAP = {
    "project": "dept_build",
    "feedback": "dept_qa",
    "reference": "dept_research",
    "user": "dept_hr",
    "standard": "dept_design",
}


def parse_memory_file(path: Path) -> dict | None:
    content = path.read_text(encoding="utf-8")
    frontmatter = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    frontmatter[key.strip()] = val.strip()
            body = parts[2].strip()
    if not body or len(body) < 20:
        return None
    mem_type = frontmatter.get("type", "")
    if not mem_type:
        for prefix in DEPT_MAP:
            if path.stem.startswith(prefix):
                mem_type = prefix
                break
    return {
        "name": frontmatter.get("name", path.stem),
        "description": frontmatter.get("description", ""),
        "type": mem_type,
        "body": body[:3000],
        "file": path.name,
        "dept": DEPT_MAP.get(mem_type, "dept_research"),
    }


async def inject_to_qdrant(records: list[dict], collection: str, execute: bool):
    if not execute:
        for rec in records:
            text = f"{rec['name']}: {rec['description']}\n\n{rec['body']}"
            print(f"  [dry-run] {rec['file'][:40]} → {collection} ({len(text)} chars)")
        return len(records)

    from src.qdrant_memory import QdrantMemory, QdrantMemoryConfig
    config = QdrantMemoryConfig(qdrant_url=QDRANT_URL, ollama_host=OLLAMA_HOST, collection=collection)
    memory = QdrantMemory(config)
    if not await memory.is_available():
        print(f"  ERROR: Qdrant ({QDRANT_URL}) 接続不可")
        return 0
    count = 0
    for rec in records:
        text = f"{rec['name']}: {rec['description']}\n\n{rec['body']}"
        metadata = {"source": f"memory_replay_{rec['file']}", "category": rec["type"], "file": rec["file"]}
        if execute:
            try:
                point_id = await memory.add(text, metadata=metadata, collection=collection)
                print(f"  + {rec['file'][:40]} → {collection} ({point_id[:8]})")
                count += 1
            except Exception as e:
                print(f"  ! {rec['file'][:40]} FAILED: {e}")
        else:
            print(f"  [dry-run] {rec['file'][:40]} → {collection} ({len(text)} chars)")
            count += 1
    return count


async def main():
    parser = argparse.ArgumentParser(description="Memory → Qdrant 再注入")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dept", action="store_true")
    parser.add_argument("--filter", default="")
    args = parser.parse_args()
    print(f"Qdrant: {QDRANT_URL} | Ollama: {OLLAMA_HOST} | Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    files = sorted(MEMORY_DIR.glob("*.md"))
    files = [f for f in files if f.name not in ("MEMORY.md", "SESSIONS_INDEX.md")]
    if args.filter:
        files = [f for f in files if args.filter in f.name]
    records = [r for f in files if (r := parse_memory_file(f))]
    print(f"Files: {len(files)} → Valid: {len(records)}")
    print(f"\n=== mem0_shared ===")
    shared = await inject_to_qdrant(records, "mem0_shared", args.execute)
    dept = 0
    if args.dept:
        groups: dict[str, list] = {}
        for rec in records:
            groups.setdefault(rec["dept"], []).append(rec)
        for d, recs in sorted(groups.items()):
            print(f"\n=== {d} ({len(recs)}) ===")
            dept += await inject_to_qdrant(recs, d, args.execute)
    print(f"\nTotal: {shared}(shared) + {dept}(dept) = {shared + dept}")
    if not args.execute:
        print("※ --execute で実投入")


if __name__ == "__main__":
    asyncio.run(main())
