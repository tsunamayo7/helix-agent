"""Contradiction Detector — memory/ファイル間の矛盾検出.

同一トピックで異なる方針・結論を持つファイルを検出し、
マージまたは更新を提案する。週次デーモンとして実行。

検出方法:
1. frontmatterのnameが類似（Levenshtein or 部分一致）
2. 同一キーワードが複数ファイルに異なる文脈で出現
3. Qdrantベクトル検索で高類似度だがcontent_hashが異なる

使い方:
    python scripts/contradiction_detector.py           # 検出実行
    python scripts/contradiction_detector.py status    # 前回結果
"""

import io
import json
import os
import re
import sys
import subprocess
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
OLLAMA_URL = "http://localhost:11434"
QDRANT_URL = "http://localhost:6333"
COLLECTION = "mem0_shared"
EMBED_MODEL = "qwen3-embedding:8b"
STATE_DIR = Path.home() / ".helix-agent" / "contradiction"
STATE_FILE = STATE_DIR / "state.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 類似度閾値
SIMILARITY_THRESHOLD = 0.85  # この値以上で類似ファイルとして検出


def parse_frontmatter(filepath: Path) -> dict:
    """frontmatterを解析."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    if not content.startswith("---"):
        return {"body": content}

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {"body": content}

    fm = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            fm[key.strip()] = val.strip()
    fm["body"] = parts[2].strip()
    return fm


def get_embedding(text: str) -> list[float]:
    """テキストの埋め込みベクトルを取得."""
    try:
        data = json.dumps({"model": EMBED_MODEL, "input": text[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embed",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode("utf-8"))
        embeddings = result.get("embeddings", [])
        return embeddings[0] if embeddings else result.get("embedding", [])
    except Exception:
        return []


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """コサイン類似度を計算."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def detect_name_similarity(files: list[dict]) -> list[dict]:
    """ファイル名の部分一致で類似ファイルを検出."""
    conflicts = []
    for i, f1 in enumerate(files):
        for f2 in files[i + 1:]:
            name1 = f1.get("name", "").lower()
            name2 = f2.get("name", "").lower()
            if not name1 or not name2:
                continue

            # 共通キーワード抽出
            words1 = set(re.findall(r'[a-z_]+', name1))
            words2 = set(re.findall(r'[a-z_]+', name2))
            common = words1 & words2 - {"md", "the", "and", "for", "project", "feedback", "user", "reference"}

            if len(common) >= 2:
                conflicts.append({
                    "type": "name_similarity",
                    "file1": f1["path"],
                    "file2": f2["path"],
                    "common_keywords": list(common),
                })
    return conflicts


def detect_vector_similarity(files: list[dict]) -> list[dict]:
    """ベクトル類似度で内容が近いファイルを検出."""
    conflicts = []
    embeddings = {}

    for f in files:
        # 各ファイルの概要テキスト
        summary = f"{f.get('name', '')} {f.get('description', '')} {f.get('body', '')[:500]}"
        vec = get_embedding(summary)
        if vec:
            embeddings[f["path"]] = vec

    paths = list(embeddings.keys())
    for i, p1 in enumerate(paths):
        for p2 in paths[i + 1:]:
            sim = cosine_similarity(embeddings[p1], embeddings[p2])
            if sim >= SIMILARITY_THRESHOLD:
                conflicts.append({
                    "type": "vector_similarity",
                    "file1": p1,
                    "file2": p2,
                    "similarity": round(sim, 3),
                })
    return conflicts


def detect_type_conflicts(files: list[dict]) -> list[dict]:
    """同一typeで重複するトピックを検出."""
    conflicts = []
    by_type = defaultdict(list)
    for f in files:
        t = f.get("type", "unknown")
        by_type[t].append(f)

    for t, group in by_type.items():
        if len(group) < 2:
            continue
        # 各ペアでdescriptionの類似をチェック
        for i, f1 in enumerate(group):
            for f2 in group[i + 1:]:
                d1 = set((f1.get("description", "") or "").lower().split())
                d2 = set((f2.get("description", "") or "").lower().split())
                if not d1 or not d2:
                    continue
                overlap = len(d1 & d2) / min(len(d1), len(d2))
                if overlap > 0.5:
                    conflicts.append({
                        "type": "type_overlap",
                        "memory_type": t,
                        "file1": f1["path"],
                        "file2": f2["path"],
                        "description_overlap": round(overlap, 2),
                    })
    return conflicts


def send_notification(message: str) -> bool:
    if WEBHOOK_SCRIPT.exists():
        try:
            result = subprocess.run(
                [sys.executable, str(WEBHOOK_SCRIPT), message],
                capture_output=True, text=True, timeout=30,
            )
            return result.returncode == 0
        except Exception:
            pass
    return False


def run_detection() -> dict:
    """矛盾検出を実行."""
    print("=== Contradiction Detector ===")
    print()

    # memory/ファイルを収集
    files = []
    for f in MEMORY_DIR.glob("*.md"):
        if f.name in ("MEMORY.md", "memory-dashboard.base"):
            continue
        fm = parse_frontmatter(f)
        fm["path"] = f.name
        files.append(fm)

    print(f"対象ファイル: {len(files)}")

    all_conflicts = []

    # 1. ファイル名類似
    print("[1/3] ファイル名類似チェック...")
    name_conflicts = detect_name_similarity(files)
    all_conflicts.extend(name_conflicts)
    print(f"  検出: {len(name_conflicts)}件")

    # 2. type重複
    print("[2/3] type内重複チェック...")
    type_conflicts = detect_type_conflicts(files)
    all_conflicts.extend(type_conflicts)
    print(f"  検出: {len(type_conflicts)}件")

    # 3. ベクトル類似度（Ollamaが必要）
    print("[3/3] ベクトル類似度チェック...")
    try:
        vector_conflicts = detect_vector_similarity(files)
        all_conflicts.extend(vector_conflicts)
        print(f"  検出: {len(vector_conflicts)}件")
    except Exception as e:
        print(f"  スキップ (Ollama未起動の可能性): {e}")

    # 結果保存
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "files_checked": len(files),
        "conflicts_found": len(all_conflicts),
        "conflicts": all_conflicts,
    }
    STATE_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # サマリ表示
    print(f"\n合計: {len(all_conflicts)}件の潜在的矛盾/重複")
    if all_conflicts:
        for c in all_conflicts:
            print(f"  [{c['type']}] {c['file1']} <-> {c['file2']}")
            if "similarity" in c:
                print(f"    類似度: {c['similarity']}")
            if "common_keywords" in c:
                print(f"    共通: {', '.join(c['common_keywords'])}")

        # Discord通知
        msg = f"🔍 **矛盾検出**: {len(all_conflicts)}件\n"
        for c in all_conflicts[:5]:
            msg += f"- [{c['type']}] {c['file1']} ↔ {c['file2']}\n"
        if len(all_conflicts) > 5:
            msg += f"...他{len(all_conflicts)-5}件"
        send_notification(msg)

    return result


def show_status():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        print(f"最終実行: {state.get('timestamp', 'unknown')}")
        print(f"チェック: {state.get('files_checked', 0)}ファイル")
        print(f"検出: {state.get('conflicts_found', 0)}件")
        for c in state.get("conflicts", []):
            print(f"  [{c['type']}] {c['file1']} <-> {c['file2']}")
    else:
        print("まだ実行されていません。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    else:
        run_detection()
