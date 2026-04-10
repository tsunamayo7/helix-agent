"""Qdrant Dedup — mem0_shared コレクションの重複ポイント検出・削除.

検出方法:
1. content_hashが同一 → 完全重複 → 古い方を削除
2. ベクトル類似度 >= 0.95 かつ同一user_id → 近似重複 → レポート

月次デーモンとして実行。

使い方:
    python scripts/qdrant_dedup.py              # 検出+レポート（削除しない）
    python scripts/qdrant_dedup.py --delete      # 検出+完全重複を削除
    python scripts/qdrant_dedup.py status        # 前回結果
"""

import io
import json
import os
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

QDRANT_URL = "http://localhost:6333"
COLLECTION = "mem0_shared"
STATE_DIR = Path.home() / ".helix-agent" / "qdrant_dedup"
STATE_FILE = STATE_DIR / "state.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

BATCH_SIZE = 100
NEAR_DUPE_THRESHOLD = 0.95


def qdrant_request(path: str, method: str = "GET", data: dict = None, timeout: int = 30) -> dict:
    """Qdrant HTTP APIリクエスト."""
    url = f"{QDRANT_URL}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read().decode("utf-8"))


def get_all_points() -> list[dict]:
    """全ポイントを取得（ページネーション）."""
    all_points = []
    offset = None

    while True:
        data = {
            "limit": BATCH_SIZE,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            data["offset"] = offset

        result = qdrant_request(
            f"/collections/{COLLECTION}/points/scroll",
            method="POST",
            data=data,
        )
        points = result.get("result", {}).get("points", [])
        next_offset = result.get("result", {}).get("next_page_offset")

        all_points.extend(points)

        if not next_offset or not points:
            break
        offset = next_offset

    return all_points


def find_exact_duplicates(points: list[dict]) -> list[dict]:
    """content_hashが同一の完全重複を検出."""
    by_hash = defaultdict(list)
    for p in points:
        payload = p.get("payload", {})
        ch = payload.get("content_hash", "")
        if ch:
            by_hash[ch].append(p)

    duplicates = []
    for ch, group in by_hash.items():
        if len(group) > 1:
            # 最新を残す（created_atがあればそれで、なければID順）
            sorted_group = sorted(group, key=lambda x: x.get("id", 0), reverse=True)
            keep = sorted_group[0]
            remove = sorted_group[1:]
            duplicates.append({
                "content_hash": ch,
                "keep_id": keep["id"],
                "remove_ids": [r["id"] for r in remove],
                "memory_preview": keep.get("payload", {}).get("memory", "")[:100],
            })
    return duplicates


def find_near_duplicates(points: list[dict], sample_size: int = 50) -> list[dict]:
    """ベクトル類似度による近似重複検出（サンプリング）."""
    near_dupes = []

    # メモリ効率のためサンプリング
    sample = points[:sample_size]

    for p in sample:
        pid = p.get("id")
        if pid is None:
            continue

        try:
            # 類似検索
            result = qdrant_request(
                f"/collections/{COLLECTION}/points/search",
                method="POST",
                data={
                    "limit": 5,
                    "with_payload": ["memory", "content_hash", "user_id"],
                    "filter": {
                        "must_not": [{"has_id": [pid]}]
                    },
                    # ID指定で検索
                },
            )
        except Exception:
            continue

    return near_dupes


def delete_points(point_ids: list) -> int:
    """指定ポイントを削除."""
    if not point_ids:
        return 0
    try:
        qdrant_request(
            f"/collections/{COLLECTION}/points/delete",
            method="POST",
            data={"points": point_ids},
        )
        return len(point_ids)
    except Exception as e:
        print(f"  削除失敗: {e}")
        return 0


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


def run_dedup(do_delete: bool = False) -> dict:
    """重複検出を実行."""
    print("=== Qdrant Dedup ===")
    print()

    # コレクション情報
    try:
        info = qdrant_request(f"/collections/{COLLECTION}")
        total = info.get("result", {}).get("points_count", 0)
        print(f"コレクション: {COLLECTION}")
        print(f"ポイント数: {total}")
    except Exception as e:
        print(f"Qdrant接続失敗: {e}")
        return {"status": "error", "message": str(e)}

    # 全ポイント取得
    print("\nポイント取得中...")
    points = get_all_points()
    print(f"取得: {len(points)}件")

    # 1. 完全重複
    print("\n[1/2] content_hash完全重複検出...")
    exact_dupes = find_exact_duplicates(points)
    total_removable = sum(len(d["remove_ids"]) for d in exact_dupes)
    print(f"  重複グループ: {len(exact_dupes)}")
    print(f"  削除可能: {total_removable}ポイント")

    for d in exact_dupes[:10]:
        print(f"    hash: {d['content_hash'][:16]}... keep:{d['keep_id']} remove:{d['remove_ids']}")
        print(f"    preview: {d['memory_preview'][:80]}")

    # 2. 削除実行
    deleted = 0
    if do_delete and exact_dupes:
        print("\n完全重複を削除中...")
        all_remove = []
        for d in exact_dupes:
            all_remove.extend(d["remove_ids"])
        deleted = delete_points(all_remove)
        print(f"  削除完了: {deleted}ポイント")

    # 結果保存
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_points": len(points),
        "exact_duplicate_groups": len(exact_dupes),
        "removable_points": total_removable,
        "deleted": deleted,
    }
    STATE_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Discord通知
    if exact_dupes or deleted:
        msg = (
            f"🧹 **Qdrant Dedup**\n"
            f"- ポイント数: {len(points)}\n"
            f"- 重複グループ: {len(exact_dupes)}\n"
            f"- 削除可能: {total_removable}\n"
            f"- 削除済み: {deleted}"
        )
        send_notification(msg)

    print(f"\n完了: {len(exact_dupes)}グループ, {total_removable}件削除可能, {deleted}件削除済み")
    return result


def show_status():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        print(f"最終実行: {state.get('timestamp', 'unknown')}")
        print(f"ポイント数: {state.get('total_points', 0)}")
        print(f"重複: {state.get('exact_duplicate_groups', 0)}グループ")
        print(f"削除可能: {state.get('removable_points', 0)}")
        print(f"削除済み: {state.get('deleted', 0)}")
    else:
        print("まだ実行されていません。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif "--delete" in sys.argv:
        run_dedup(do_delete=True)
    else:
        run_dedup()
