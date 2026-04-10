"""Helix Corp — 部門別Qdrantコレクション初期化.

6つの部門コレクションをQdrantに作成する。
既存の mem0_shared には一切触れない。

使い方:
    python scripts/init_dept_collections.py           # 初期化
    python scripts/init_dept_collections.py status     # 状態確認
    python scripts/init_dept_collections.py delete     # 全部門コレクション削除(復旧用)
"""

import io
import json
import os
import sys
import urllib.request
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

QDRANT_URL = "http://localhost:6333"
EMBED_DIM = 4096  # qwen3-embedding:8b

DEPARTMENTS = {
    "dept_hr": "人事/採用部門 — 転職市場、面接パターン、スキル評価、プロフィール最適化",
    "dept_research": "調査研究部門 — 技術調査結果、競合分析、トレンド、最新動向",
    "dept_design": "設計部門 — ADR、設計判断根拠、アーキテクチャパターン、技術選定",
    "dept_build": "構築部門 — 実装パターン、バグ修正履歴、テスト戦略、CI/CD",
    "dept_qa": "品質管理部門 — 脆弱性パターン、レビュー指摘集、品質基準、セキュリティ",
}


def qdrant_request(path: str, method: str = "GET", data: dict = None, timeout: int = 10) -> dict:
    url = f"{QDRANT_URL}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode("utf-8")}
    except Exception as e:
        return {"error": str(e)}


def collection_exists(name: str) -> bool:
    result = qdrant_request(f"/collections/{name}")
    return "error" not in result


def create_collection(name: str, description: str) -> bool:
    if collection_exists(name):
        print(f"  {name}: 既に存在 (スキップ)")
        return True

    result = qdrant_request(f"/collections/{name}", method="PUT", data={
        "vectors": {
            "size": EMBED_DIM,
            "distance": "Cosine",
        },
    })

    if "error" not in result:
        print(f"  {name}: 作成完了 ✅ — {description}")
        return True
    else:
        print(f"  {name}: 作成失敗 ❌ — {result}")
        return False


def delete_collection(name: str) -> bool:
    result = qdrant_request(f"/collections/{name}", method="DELETE")
    if "error" not in result:
        print(f"  {name}: 削除完了")
        return True
    else:
        print(f"  {name}: 削除失敗 — {result}")
        return False


def init_all():
    print("=== Helix Corp — 部門コレクション初期化 ===")
    print(f"Qdrant: {QDRANT_URL}")
    print(f"ベクトル次元: {EMBED_DIM}")
    print()

    # mem0_shared の存在確認（安全チェック）
    if not collection_exists("mem0_shared"):
        print("⚠️ mem0_shared が存在しません。先にQdrant Memory Serverを起動してください。")
        return False

    print("[既存] mem0_shared: OK")
    print()
    print("[部門コレクション作成]")

    success = 0
    for name, desc in DEPARTMENTS.items():
        if create_collection(name, desc):
            success += 1

    print(f"\n完了: {success}/{len(DEPARTMENTS)} コレクション作成")
    return success == len(DEPARTMENTS)


def show_status():
    print("=== 部門コレクション状態 ===")
    print()

    # mem0_shared
    if collection_exists("mem0_shared"):
        info = qdrant_request("/collections/mem0_shared")
        count = info.get("result", {}).get("points_count", "?")
        print(f"  mem0_shared (全社共有): {count} ポイント ✅")
    else:
        print(f"  mem0_shared: 不在 ❌")

    # 部門
    for name, desc in DEPARTMENTS.items():
        if collection_exists(name):
            info = qdrant_request(f"/collections/{name}")
            count = info.get("result", {}).get("points_count", "?")
            print(f"  {name}: {count} ポイント ✅")
        else:
            print(f"  {name}: 未作成 ❌")


def delete_all():
    print("=== 部門コレクション削除 ===")
    print("⚠️ mem0_shared は削除しません")
    print()
    for name in DEPARTMENTS:
        delete_collection(name)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif len(sys.argv) > 1 and sys.argv[1] == "delete":
        confirm = input("全部門コレクションを削除しますか？ (yes/no): ")
        if confirm.lower() == "yes":
            delete_all()
        else:
            print("キャンセル")
    else:
        init_all()
