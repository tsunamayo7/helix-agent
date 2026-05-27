"""Qdrant コレクション sparse vector マイグレーション.

既存コレクション (unnamed dense vector) を named vectors + sparse vector 構成に変換する。
Qdrant 1.17.0 は既存コレクションへの sparse field 後付け追加を未サポートのため、
新コレクションを作成してデータを移行する。

使い方:
    python3 scripts/migrate_sparse.py --collection dept_build [--dry-run] [--batch-size 100]
    python3 scripts/migrate_sparse.py --all-dept [--dry-run]
    python3 scripts/migrate_sparse.py --collection mem0_shared [--dry-run] [--batch-size 100]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable

# src パッケージを import 可能にする
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx

from src.qdrant_memory import QdrantMemory, QdrantMemoryConfig

# ── 定数 ──

DEPT_COLLECTIONS = [
    "dept_build",
    "dept_research",
    "dept_design",
    "dept_qa",
    "dept_hr",
]

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "").strip()

# batch-size 上限 (Qdrant REST API の安全範囲)
MAX_BATCH_SIZE = 500


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        h["api-key"] = QDRANT_API_KEY
    return h


# ── Qdrant HTTP helpers ──


async def qdrant_get(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.get(f"{QDRANT_URL}{path}")
    r.raise_for_status()
    return r.json()


async def qdrant_post(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    r = await client.post(f"{QDRANT_URL}{path}", json=payload)
    r.raise_for_status()
    return r.json()


async def qdrant_put(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    r = await client.put(f"{QDRANT_URL}{path}", json=payload)
    r.raise_for_status()
    return r.json()


async def qdrant_delete(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.delete(f"{QDRANT_URL}{path}")
    r.raise_for_status()
    return r.json()


# ── コレクション情報取得 ──


async def get_collection_info(client: httpx.AsyncClient, name: str) -> dict | None:
    """コレクション情報を取得。存在しなければ None."""
    try:
        data = await qdrant_get(client, f"/collections/{name}")
        return data.get("result", {})
    except httpx.HTTPStatusError:
        return None


async def get_points_count(client: httpx.AsyncClient, name: str) -> int:
    """コレクションのポイント数を取得."""
    info = await get_collection_info(client, name)
    if info is None:
        return -1
    return info.get("points_count", 0)


# ── ストリーミングスクロール ──


async def scroll_and_process(
    client: httpx.AsyncClient,
    collection: str,
    batch_size: int,
    callback: Callable[[list[dict]], Awaitable[None]],
    total_hint: int = 0,
    label: str = "scroll",
) -> int:
    """コレクションをスクロールしながらバッチ単位で callback を呼ぶ。

    全件をメモリに保持しない。callback にはバッチ (list[dict]) が渡される。
    戻り値は処理した総ポイント数。
    """
    offset: str | int | None = None
    processed = 0
    batch_num = 0

    while True:
        payload: dict = {
            "limit": batch_size,
            "with_payload": True,
            "with_vector": True,
        }
        if offset is not None:
            payload["offset"] = offset

        data = await qdrant_post(client, f"/collections/{collection}/points/scroll", payload)
        result = data.get("result", {})
        points = result.get("points", [])
        next_offset = result.get("next_page_offset")

        if points:
            await callback(points)
            processed += len(points)
            batch_num += 1
            hint_str = f"/{total_hint}" if total_hint else ""
            print(f"  {label}: {processed}{hint_str} ポイント処理済み (batch {batch_num})")

        if not points or next_offset is None:
            break
        offset = next_offset

    return processed


# ── テキスト抽出 ──


def extract_text(payload: dict) -> str:
    """payload からテキストを抽出. data > text > memory の優先順."""
    return payload.get("data", payload.get("text", payload.get("memory", "")))


# ── vector 変換 ──


def convert_vector(point: dict, sparse_encoder: QdrantMemory) -> dict:
    """元のポイントから named vectors (dense + sparse) を生成.

    元のコレクションは unnamed vector (直接 vector: [...]) なので、
    named vectors 形式 {"dense": [...], "sparse": {...}} に変換する。
    """
    # 元の vector を取得 (unnamed = list[float], named = dict)
    raw_vector = point.get("vector", [])

    # unnamed vector (list) の場合はそのまま dense に
    if isinstance(raw_vector, list):
        dense = raw_vector
    elif isinstance(raw_vector, dict):
        # 既に named の場合 (dense キーがあればそれを使用)
        dense = raw_vector.get("dense", raw_vector.get("", []))
    else:
        dense = []

    # sparse vector 生成 (パブリック API)
    text = extract_text(point.get("payload", {}))
    indices, values = sparse_encoder.sparse_encode(text)

    result: dict = {"dense": dense}
    if indices:
        result["sparse"] = {"indices": indices, "values": values}

    return result


# ── indexing 待ち ──


async def wait_for_indexing(
    client: httpx.AsyncClient,
    collection: str,
    expected: int,
    wait_timeout: int,
    label: str = "",
) -> int:
    """indexing が完了するまで待機。expected 以上になったポイント数を返す。"""
    deadline = time.monotonic() + wait_timeout
    interval = 2  # 初回 2秒
    max_interval = 30

    while time.monotonic() < deadline:
        count = await get_points_count(client, collection)
        if count >= expected:
            return count
        remaining = int(deadline - time.monotonic())
        tag = f" ({label})" if label else ""
        print(f"  indexing 待ち{tag}... ({count}/{expected}, 残り{remaining}秒)")
        await asyncio.sleep(min(interval, max(1, remaining)))
        interval = min(interval * 1.5, max_interval)

    # タイムアウト — 最終値を返す
    return await get_points_count(client, collection)


# ── マイグレーション本体 ──


async def migrate_collection(
    name: str,
    batch_size: int,
    dry_run: bool,
    wait_timeout: int = 300,
) -> bool:
    """単一コレクションのマイグレーションを実行."""
    v2_name = f"{name}_v2"
    old_name = f"{name}_old"
    sparse_encoder = QdrantMemory(QdrantMemoryConfig())

    print(f"\n{'='*60}")
    print(f"マイグレーション: {name}")
    print(f"{'='*60}")

    async with httpx.AsyncClient(timeout=60.0, headers=_headers()) as client:
        # ── Step 1: 元コレクションの設定を取得 ──
        print(f"\n[1/9] 元コレクション情報取得: {name}")
        info = await get_collection_info(client, name)
        if info is None:
            print(f"  エラー: コレクション {name} が存在しません")
            return False

        original_count = info.get("points_count", 0)
        config = info.get("config", {})
        params = config.get("params", {})
        vectors_config = params.get("vectors", {})
        sparse_config = params.get("sparse_vectors", {})

        print(f"  ポイント数: {original_count}")
        print(f"  vectors_config: {vectors_config}")
        print(f"  sparse_vectors: {sparse_config}")

        # 既に sparse field がある場合はスキップ
        if "sparse" in sparse_config:
            print(f"  スキップ: {name} には既に sparse field が存在します")
            return True

        if original_count == 0:
            print(f"  スキップ: {name} にはポイントがありません")
            return True

        # 元の vector 設定を取得
        # unnamed vector の場合: {"size": N, "distance": "Cosine"}
        # named vector の場合: {"dense": {"size": N, "distance": "Cosine"}}
        if "size" in vectors_config:
            # unnamed vector
            embed_dim = vectors_config["size"]
            distance = vectors_config.get("distance", "Cosine")
        elif "dense" in vectors_config:
            # named vector
            embed_dim = vectors_config["dense"]["size"]
            distance = vectors_config["dense"].get("distance", "Cosine")
        else:
            # その他 (最初のキーを使用)
            first_key = next(iter(vectors_config), None)
            if first_key and isinstance(vectors_config[first_key], dict):
                embed_dim = vectors_config[first_key]["size"]
                distance = vectors_config[first_key].get("distance", "Cosine")
            else:
                print(f"  エラー: vectors_config の形式が不明: {vectors_config}")
                return False

        print(f"  次元: {embed_dim}, 距離: {distance}")

        # ── Step 2: v2 コレクション作成 ──
        print(f"\n[2/9] v2 コレクション作成: {v2_name}")

        # v2 が既に存在する場合は確認
        v2_info = await get_collection_info(client, v2_name)
        if v2_info is not None:
            v2_count = v2_info.get("points_count", 0)
            print(f"  警告: {v2_name} が既に存在します ({v2_count} ポイント)")
            print(f"  削除して再作成します")
            if not dry_run:
                await qdrant_delete(client, f"/collections/{v2_name}")

        v2_config = {
            "vectors": {
                "dense": {
                    "size": embed_dim,
                    "distance": distance,
                }
            },
            "sparse_vectors": {
                "sparse": {}
            },
        }

        if dry_run:
            print(f"  [DRY-RUN] コレクション作成をスキップ")
            print(f"  設定: {v2_config}")
        else:
            await qdrant_put(client, f"/collections/{v2_name}", v2_config)
            print(f"  作成完了")

        # ── Step 3-5: ストリーミングで sparse vector 生成 + v2 に upsert ──
        # 旧: Step 3 で全件メモリ保持 → Step 4-5 で変換+upsert
        # 新: scroll_and_process で 1 バッチずつ変換+upsert (メモリ = batch_size 分のみ)
        print(f"\n[3-5/9] ストリーミング sparse vector 生成 + upsert")

        upsert_stats = {"upserted": 0, "sparse_generated": 0, "sparse_empty": 0}

        async def upsert_batch(batch: list[dict]) -> None:
            """1バッチ分を変換して v2 に upsert."""
            converted_points = []
            for point in batch:
                new_vectors = convert_vector(point, sparse_encoder)
                if "sparse" in new_vectors:
                    upsert_stats["sparse_generated"] += 1
                else:
                    upsert_stats["sparse_empty"] += 1

                converted_points.append({
                    "id": point["id"],
                    "vector": new_vectors,
                    "payload": point.get("payload", {}),
                })

            if not dry_run:
                await qdrant_put(
                    client,
                    f"/collections/{v2_name}/points",
                    {"points": converted_points},
                )
            upsert_stats["upserted"] += len(converted_points)

        total_processed = await scroll_and_process(
            client, name, batch_size, upsert_batch,
            total_hint=original_count,
            label="upsert",
        )

        print(f"  処理完了: {total_processed} ポイント "
              f"(sparse生成: {upsert_stats['sparse_generated']}, "
              f"空: {upsert_stats['sparse_empty']})")

        if total_processed != original_count:
            print(f"  警告: 処理数 ({total_processed}) != 元のポイント数 ({original_count})")
            print(f"  (indexed vs total の差異、または進行中の書き込みの可能性)")

        # ── Step 6: ポイント数検証 ──
        print(f"\n[6/9] ポイント数検証")

        if dry_run:
            print(f"  [DRY-RUN] 検証スキップ (元: {original_count}, 処理: {upsert_stats['upserted']})")
        else:
            v2_count = await wait_for_indexing(
                client, v2_name, total_processed, wait_timeout, label="v2 indexing"
            )
            if v2_count != total_processed:
                print(f"  エラー: ポイント数不一致 (v2: {v2_count}, 元: {total_processed})")
                print(f"  中断: {v2_name} を手動で確認してください")
                return False
            print(f"  一致: {v2_count} == {total_processed}")

        # ── Step 7: 元コレクションを old にバックアップ ──
        print(f"\n[7/9] 元コレクションを {old_name} にバックアップ")

        if dry_run:
            print(f"  [DRY-RUN] {name} -> {old_name} (削除+再作成+ストリーミングコピー)")
        else:
            # old が既に存在する場合は削除
            old_info = await get_collection_info(client, old_name)
            if old_info is not None:
                print(f"  既存の {old_name} を削除")
                await qdrant_delete(client, f"/collections/{old_name}")

            # old コレクションを作成 (元と同じ unnamed vector 形式)
            old_config: dict = {
                "vectors": vectors_config,
            }
            if sparse_config:
                old_config["sparse_vectors"] = sparse_config
            await qdrant_put(client, f"/collections/{old_name}", old_config)

            # ストリーミングで元データを old にコピー
            print(f"  {name} -> {old_name} にストリーミングコピー中...")

            async def backup_batch(batch: list[dict]) -> None:
                backup_points = []
                for point in batch:
                    backup_points.append({
                        "id": point["id"],
                        "vector": point.get("vector", []),
                        "payload": point.get("payload", {}),
                    })
                await qdrant_put(
                    client,
                    f"/collections/{old_name}/points",
                    {"points": backup_points},
                )

            backup_count = await scroll_and_process(
                client, name, batch_size, backup_batch,
                total_hint=original_count,
                label="backup",
            )

            # old のポイント数検証
            old_count = await wait_for_indexing(
                client, old_name, backup_count, wait_timeout, label="backup indexing"
            )
            if old_count != backup_count:
                print(f"  エラー: backup ポイント数不一致 (old: {old_count}, 元: {backup_count})")
                print(f"  中断: 元コレクション {name} は変更されていません")
                return False
            print(f"  backup 検証 OK: {old_count} ポイント")

        # ── Step 8: リネーム (元を削除 + v2 を元の名前で再作成) ──
        # 安全弁: 中断時に {name}_old から自動ロールバック
        print(f"\n[8/9] リネーム: {name} 削除 -> {v2_name} を {name} に")
        print(f"  安全弁: 失敗時は {old_name} から自動ロールバックします")

        if dry_run:
            print(f"  [DRY-RUN] {name} を削除し、{v2_name} のデータで {name} を再作成")
            print(f"  [DRY-RUN] 失敗時ロールバック: {old_name} -> {name} にデータ復元")
        else:
            try:
                # 元コレクションを削除
                await qdrant_delete(client, f"/collections/{name}")
                print(f"  {name} 削除完了")

                # v2 のデータで元の名前のコレクションを作成
                await qdrant_put(client, f"/collections/{name}", v2_config)
                print(f"  {name} (named vectors + sparse) 作成完了")

                # v2 -> name にストリーミングコピー
                print(f"  {v2_name} -> {name} にストリーミングコピー中...")

                async def rename_copy_batch(batch: list[dict]) -> None:
                    copy_points = []
                    for point in batch:
                        copy_points.append({
                            "id": point["id"],
                            "vector": point.get("vector", {}),
                            "payload": point.get("payload", {}),
                        })
                    await qdrant_put(
                        client,
                        f"/collections/{name}/points",
                        {"points": copy_points},
                    )

                rename_count = await scroll_and_process(
                    client, v2_name, batch_size, rename_copy_batch,
                    total_hint=upsert_stats["upserted"],
                    label="copy",
                )

                # v2 コレクションを削除
                await qdrant_delete(client, f"/collections/{v2_name}")
                print(f"  {v2_name} 削除完了")

            except Exception as step8_err:
                print(f"\n  *** Step 8 エラー: {step8_err}")
                print(f"  *** {old_name} から自動ロールバックを開始します...")

                try:
                    # ロールバック: {name} が存在しない場合は作成する
                    live_info = await get_collection_info(client, name)
                    if live_info is not None:
                        # 中途半端な状態のコレクションを削除
                        await qdrant_delete(client, f"/collections/{name}")
                        print(f"  ロールバック: 中途半端な {name} を削除")

                    # 元の形式でコレクションを再作成
                    # unnamed vector の場合、Qdrant PUT API は named 形式を要求する
                    if isinstance(vectors_config, dict) and "size" in vectors_config:
                        restore_vectors = {"dense": vectors_config}
                    else:
                        restore_vectors = vectors_config
                    restore_config: dict = {
                        "vectors": restore_vectors,
                    }
                    if sparse_config:
                        restore_config["sparse_vectors"] = sparse_config
                    await qdrant_put(client, f"/collections/{name}", restore_config)
                    print(f"  ロールバック: {name} を再作成")

                    # {name}_old からストリーミングでデータ復元
                    async def restore_batch(batch: list[dict]) -> None:
                        restore_points = []
                        for point in batch:
                            restore_points.append({
                                "id": point["id"],
                                "vector": point.get("vector", []),
                                "payload": point.get("payload", {}),
                            })
                        await qdrant_put(
                            client,
                            f"/collections/{name}/points",
                            {"points": restore_points},
                        )

                    restore_total = await scroll_and_process(
                        client, old_name, batch_size, restore_batch,
                        total_hint=original_count,
                        label="restore",
                    )
                    print(f"  ロールバック: {old_name} から {restore_total} ポイント復元")

                    # 復元後のポイント数検証
                    restored_count = await wait_for_indexing(
                        client, name, restore_total, wait_timeout, label="restore indexing"
                    )

                    if restored_count == restore_total:
                        print(f"  *** ロールバック成功: {name} を {restored_count} ポイントで復元")
                    else:
                        print(f"  *** ロールバック警告: ポイント数不一致 (復元: {restored_count}, 元: {restore_total})")
                        print(f"  *** {old_name} のデータは保持されています。手動リストアしてください")

                    # ロールバック成功後、v2_name が残っていれば削除
                    v2_leftover = await get_collection_info(client, v2_name)
                    if v2_leftover is not None:
                        await qdrant_delete(client, f"/collections/{v2_name}")
                        print(f"  ロールバック後クリーンアップ: {v2_name} を削除")

                except Exception as rollback_err:
                    print(f"  *** ロールバック失敗: {rollback_err}")
                    print(f"  *** 手動復旧が必要です:")
                    print(f"  ***   1. {old_name} のデータを確認")
                    print(f"  ***   2. {old_name} -> {name} にデータをコピー")
                    print(f"  ***   3. {v2_name} は参照用に残っています")

                return False

        # ── Step 9: 最終検証 ──
        print(f"\n[9/9] 最終検証")

        if dry_run:
            print(f"  [DRY-RUN] 全ステップ完了 (変更なし)")
            print(f"  サマリー:")
            print(f"    元ポイント数: {original_count}")
            print(f"    sparse 生成: {upsert_stats['sparse_generated']}")
            print(f"    sparse 空: {upsert_stats['sparse_empty']}")
        else:
            final_count = await get_points_count(client, name)
            final_info = await get_collection_info(client, name)
            final_sparse = final_info.get("config", {}).get("params", {}).get("sparse_vectors", {})

            if final_count != total_processed:
                print(f"  エラー: 最終ポイント数不一致 (最終: {final_count}, 元: {total_processed})")
                print(f"  復旧: {old_name} からリストアしてください")
                return False

            print(f"  ポイント数: {final_count} (元: {total_processed})")
            print(f"  sparse_vectors: {final_sparse}")
            print(f"  sparse 生成: {upsert_stats['sparse_generated']}, 空: {upsert_stats['sparse_empty']}")
            print(f"  バックアップ: {old_name} に保持 (手動削除してください)")

    print(f"\n完了: {name} のマイグレーション {'(DRY-RUN)' if dry_run else '成功'}")
    return True


# ── CLI ──


async def main():
    parser = argparse.ArgumentParser(
        description="Qdrant コレクションに sparse vector を追加するマイグレーション"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--collection", type=str, help="マイグレーション対象のコレクション名")
    group.add_argument("--all-dept", action="store_true", help="全 dept_* コレクションをマイグレーション")
    parser.add_argument("--batch-size", type=int, default=100, help="バッチサイズ (デフォルト: 100, 上限: 500)")
    parser.add_argument("--dry-run", action="store_true", help="変更を加えずにシミュレーション")
    parser.add_argument("--wait-timeout", type=int, default=300, help="indexing 待機タイムアウト秒 (デフォルト: 300)")

    args = parser.parse_args()

    # P3: batch-size クランプ
    batch_size = max(1, min(args.batch_size, MAX_BATCH_SIZE))
    if batch_size != args.batch_size:
        print(f"注意: batch-size を {args.batch_size} -> {batch_size} にクランプしました (範囲: 1-{MAX_BATCH_SIZE})")

    print("=" * 60)
    print("Qdrant Sparse Vector マイグレーション")
    print("=" * 60)
    print(f"Qdrant URL: {QDRANT_URL}")
    print(f"バッチサイズ: {batch_size}")
    print(f"indexing タイムアウト: {args.wait_timeout}秒")
    print(f"モード: {'DRY-RUN (変更なし)' if args.dry_run else '本番実行'}")
    print(f"日時: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if args.all_dept:
        collections = DEPT_COLLECTIONS
        print(f"対象: {', '.join(collections)}")
    else:
        collections = [args.collection]
        print(f"対象: {args.collection}")

    results: dict[str, bool] = {}

    for coll in collections:
        try:
            success = await migrate_collection(coll, batch_size, args.dry_run, args.wait_timeout)
            results[coll] = success
        except Exception as e:
            print(f"\nエラー: {coll} のマイグレーション中に例外: {e}")
            results[coll] = False

    # ── 結果サマリー ──
    print(f"\n{'='*60}")
    print("結果サマリー")
    print(f"{'='*60}")
    for coll, success in results.items():
        status = "OK" if success else "FAILED"
        print(f"  {coll}: {status}")

    failed = [c for c, s in results.items() if not s]
    if failed:
        print(f"\n失敗: {', '.join(failed)}")
        print("復旧: {name}_old が存在する場合、手動でリストアしてください")
        sys.exit(1)
    else:
        print(f"\n全コレクションのマイグレーション {'(DRY-RUN)' if args.dry_run else '完了'}")


if __name__ == "__main__":
    asyncio.run(main())
