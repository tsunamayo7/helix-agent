"""Department Feed Bridge — x-feed-collector/x_monitorのデータを部門RAGに投入.

x-feed-collectorはmem0_sharedに蓄積済み。このスクリプトは:
1. mem0_sharedから高品質データをフィルタリング
2. カテゴリに基づいて適切な部門コレクションに投入
3. 品質スコアフィルタでRAG汚染を防止
4. 部門情報収集リクエストの定期実行

使い方:
    python scripts/dept_feed_bridge.py              # フィード投入
    python scripts/dept_feed_bridge.py requests      # 承認済みリクエスト一覧
    python scripts/dept_feed_bridge.py status        # 状態確認
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

QDRANT_URL = "http://localhost:6333"
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "qwen3-embedding:8b"
CLASSIFY_MODEL = "gemma4:31b"  # カテゴリ自動判定用
STATE_DIR = Path.home() / ".helix-agent" / "dept_feed"
STATE_FILE = STATE_DIR / "state.json"
REQUESTS_FILE = STATE_DIR / "approved_requests.json"
LLM_CACHE_FILE = STATE_DIR / "llm_classify_cache.json"  # 同じテキストの再判定を防ぐ
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# カテゴリ→部門マッピング (x-feed-collector用)
# x-feed-collector実データのcategory値に対応 (config.yamlで定義)
CATEGORY_TO_DEPT = {
    # 研究部門 (調査・情報収集系)
    "MCP": "dept_research",
    "Claude": "dept_research",
    "LocalLLM": "dept_research",
    "JapaneseLLM": "dept_research",
    "AIAgent": "dept_research",
    "Agent": "dept_research",
    "RAG": "dept_research",
    "GenAI": "dept_research",
    "Automation": "dept_research",
    "VTuber": "dept_research",  # VTuber関連技術情報
    # 構築部門 (実装・開発系)
    "Dev": "dept_build",
    "DevOps": "dept_build",
    "Hardware": "dept_build",
    "ImageGen": "dept_build",
    "VideoGen": "dept_build",
    # 品質管理部門 (テスト・セキュリティ系)
    "Security": "dept_qa",
    "Testing": "dept_qa",
    # 設計部門 (アーキテクチャ系)
    "Architecture": "dept_design",
    # 人事部門 (キャリア系)
    "Career": "dept_hr",
    "Job": "dept_hr",
}

# x-feed-collectorの source フィールドのバリエーション
# qdrant_store.pyで x-feed-collector-twitter / -yahoo-realtime / (無印) 等
X_FEED_SOURCES = [
    "x-feed-collector",
    "x-feed-collector-twitter",
    "x-feed-collector-yahoo-realtime",
    "x-feed-collector-reddit",
    "x-feed-collector-hn",
    "x-feed-collector-github",
]

# cmem_bridge type→部門デフォルト (キーワード判定で上書きされる)
CMEM_TYPE_TO_DEPT = {
    "bugfix": "dept_build",
    "feature": "dept_build",
    "refactor": "dept_build",
    "change": "dept_build",
    "discovery": "dept_research",
    "decision": "dept_design",
}

# キーワードベースの部門判定 (type判定を上書き)
KEYWORD_TO_DEPT = {
    "dept_qa": [
        "security", "vulnerability", "auth", "authentication", "authorization",
        "owasp", "injection", "xss", "csrf", "exploit", "credential", "secret",
        "encryption", "cve", "sanitize", "validation", "脆弱", "セキュリティ",
        "test", "pytest", "coverage", "unittest", "テスト",
    ],
    "dept_design": [
        "architecture", "design pattern", "refactor", "abstract", "interface",
        "schema", "api design", "workflow", "アーキテクチャ", "設計",
        "scalability", "module", "microservice", "dependency",
    ],
    "dept_build": [
        "implementation", "build", "compile", "deploy", "ci/cd", "docker",
        "pipeline", "dockerfile", "yaml", "github action", "実装", "ビルド",
        "install", "setup", "configuration", "script",
    ],
    "dept_hr": [
        "interview", "job", "career", "salary", "resume", "hiring", "候補者",
        "面接", "採用", "転職", "候補", "hr",
    ],
}

# 品質フィルタ閾値
MIN_RELEVANCE_SCORE = 5     # x-feed-collectorのRelevance 1-10
MIN_VECTOR_SCORE = 0.65     # Qdrant検索スコア
MAX_AGE_DAYS = 7            # 7日以内のデータのみ投入
CMEM_BATCH_SIZE = 200       # cmem_bridgeバッチ投入件数


def qdrant_request(path: str, method: str = "POST", data: dict = None, timeout: int = 15) -> dict:
    url = f"{QDRANT_URL}{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def get_embedding(text: str) -> list[float]:
    try:
        data = json.dumps({"model": EMBED_MODEL, "input": text[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embed", data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode("utf-8"))
        embeddings = result.get("embeddings", [])
        return embeddings[0] if embeddings else result.get("embedding", [])
    except Exception:
        return []


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_run": None, "synced_hashes": [], "stats": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_approved_requests() -> list[dict]:
    if REQUESTS_FILE.exists():
        try:
            return json.loads(REQUESTS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_approved_requests(requests: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REQUESTS_FILE.write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def fetch_recent_from_shared(category_filter: str = None, limit: int = 50) -> list[dict]:
    """mem0_sharedから最近の高品質データを取得.

    x-feed-collectorは source フィールドに以下のバリエーションがある:
      - x-feed-collector (Reddit/HN/GitHub等)
      - x-feed-collector-twitter
      - x-feed-collector-yahoo-realtime
    should句でOR検索する (Qdrant の match any 相当)
    """
    scroll_data = {
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
        "filter": {
            "must": [
                {"key": "user_id", "match": {"value": "tsunamayo7"}},
                # source が X_FEED_SOURCES のいずれかにマッチ (Qdrant MatchAny)
                {"key": "source", "match": {"any": X_FEED_SOURCES}},
            ]
        },
    }
    if category_filter:
        scroll_data["filter"]["must"].append(
            {"key": "category", "match": {"value": category_filter}}
        )

    result = qdrant_request("/collections/mem0_shared/points/scroll", data=scroll_data)
    return result.get("result", {}).get("points", [])


def fetch_cmem_bridge_batch(offset=None, limit: int = 200) -> tuple[list[dict], object]:
    """cmem_bridge由来データをバッチ取得 (ページング対応).

    Returns:
        (points, next_offset)
    """
    scroll_data = {
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
        "filter": {
            "must": [
                {"key": "user_id", "match": {"value": "tsunamayo7"}},
                {"key": "source", "match": {"value": "cmem_bridge"}},
            ]
        },
    }
    if offset is not None:
        scroll_data["offset"] = offset

    result = qdrant_request("/collections/mem0_shared/points/scroll", data=scroll_data)
    r = result.get("result", {})
    return r.get("points", []), r.get("next_page_offset")


def load_llm_cache() -> dict:
    """LLM分類キャッシュを読み込み (text_hash → dept)."""
    if LLM_CACHE_FILE.exists():
        try:
            return json.loads(LLM_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_llm_cache(cache: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 最新2000件のみ保持
    if len(cache) > 2000:
        cache = dict(list(cache.items())[-2000:])
    LLM_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def classify_with_llm(text: str, cache: dict | None = None) -> str:
    """gemma4で部門を自動判定 (最終手段).

    5部門: dept_research/dept_design/dept_build/dept_qa/dept_hr
    判定不能なら dept_research (デフォルト)
    """
    if cache is None:
        cache = load_llm_cache()

    ch = content_hash(text)
    if ch in cache:
        return cache[ch]

    prompt = (
        "以下のテキストを5部門のいずれかに分類してください。\n"
        "- dept_research: 調査・研究・技術情報・トレンド\n"
        "- dept_design: アーキテクチャ・設計パターン・拡張性議論\n"
        "- dept_build: 実装・ビルド・DevOps・ツール\n"
        "- dept_qa: セキュリティ・テスト・脆弱性\n"
        "- dept_hr: 採用・キャリア・転職\n\n"
        f"テキスト:\n{text[:800]}\n\n"
        "回答は部門名のみ (例: dept_research) 。他の文字は含めないでください。"
    )

    try:
        data = json.dumps({
            "model": CLASSIFY_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 20},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read().decode("utf-8"))
        response_text = result.get("response", "").strip().lower()

        # 部門名を抽出
        valid_depts = ["dept_research", "dept_design", "dept_build", "dept_qa", "dept_hr"]
        for d in valid_depts:
            if d in response_text:
                cache[ch] = d
                return d
    except Exception:
        pass

    cache[ch] = "dept_research"
    return "dept_research"


def classify_cmem_to_dept(text: str, cmem_type: str) -> str:
    """cmem_bridgeデータのテキスト+typeから部門を自動判定.

    優先順位:
    1. キーワードマッチ (KEYWORD_TO_DEPT): 最初にヒットした部門
    2. type デフォルト (CMEM_TYPE_TO_DEPT)
    3. フォールバック: dept_research
    """
    text_lower = text.lower()

    # キーワード判定 (優先順位: qa > design > build > hr)
    for dept in ("dept_qa", "dept_design", "dept_build", "dept_hr"):
        keywords = KEYWORD_TO_DEPT.get(dept, [])
        for kw in keywords:
            if kw.lower() in text_lower:
                return dept

    # type デフォルト
    if cmem_type in CMEM_TYPE_TO_DEPT:
        return CMEM_TYPE_TO_DEPT[cmem_type]

    return "dept_research"


def filter_quality(points: list[dict], state: dict) -> list[dict]:
    """品質フィルタ: スコアとハッシュで重複・低品質を除外."""
    synced = set(state.get("synced_hashes", []))
    filtered = []

    for p in points:
        payload = p.get("payload", {})
        text = payload.get("data", payload.get("memory", ""))
        if not text:
            continue

        ch = content_hash(text)
        if ch in synced:
            continue  # 既に投入済み

        # スコアフィルタ（x-feed-collectorのrelevanceがpayloadにある場合）
        relevance = payload.get("relevance_score", 5)
        if isinstance(relevance, (int, float)) and relevance < MIN_RELEVANCE_SCORE:
            continue

        filtered.append({
            "text": text,
            "hash": ch,
            "category": payload.get("category", ""),
            "source": payload.get("source", ""),
            "link": payload.get("link", ""),
            "created_at": payload.get("created_at", ""),
        })

    return filtered


def upsert_to_dept(collection: str, text: str, metadata: dict) -> bool:
    """部門コレクションにポイントを追加."""
    vector = get_embedding(text)
    if not vector:
        return False

    import uuid
    point_id = str(uuid.uuid4())

    payload = {
        "data": text,
        "user_id": "tsunamayo7",
        "department": collection,
        "source": "dept_feed_bridge",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload.update(metadata)

    result = qdrant_request(
        f"/collections/{collection}/points",
        method="PUT",
        data={"points": [{"id": point_id, "vector": vector, "payload": payload}]},
    )
    return "error" not in result


def run_unknown_category_feed(state: dict) -> dict:
    """CATEGORY_TO_DEPT にマッチしない x-feed-collector データを LLM判定で投入."""
    print("--- x-feed-collector unknown category → LLM classify ---")
    synced = set(state.get("synced_hashes", []))
    stats = {"total": 0, "synced": {}, "skipped": 0}
    llm_cache = load_llm_cache()

    # x-feed-collector由来のデータを全取得 (カテゴリフィルタなし)
    try:
        scroll_data = {
            "limit": 500,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": [
                    {"key": "user_id", "match": {"value": "tsunamayo7"}},
                    {"key": "source", "match": {"any": X_FEED_SOURCES}},
                ]
            },
        }
        result = qdrant_request("/collections/mem0_shared/points/scroll", data=scroll_data)
        points = result.get("result", {}).get("points", [])
    except Exception as e:
        print(f"  取得失敗: {e}")
        return stats

    known_categories = set(CATEGORY_TO_DEPT.keys())

    for p in points:
        payload = p.get("payload", {})
        category = payload.get("category", "")
        # 既にマッピングがあるカテゴリはスキップ (通常の run_feed で処理)
        if category in known_categories:
            continue

        text = payload.get("memory", payload.get("data", ""))
        if not text or len(text) < 30:
            stats["skipped"] += 1
            continue

        ch = content_hash(text)
        if ch in synced:
            stats["skipped"] += 1
            continue

        # 品質フィルタ: relevance_score
        relevance = payload.get("relevance_score", 5)
        if isinstance(relevance, (int, float)) and relevance < MIN_RELEVANCE_SCORE:
            stats["skipped"] += 1
            continue

        # LLMで部門判定
        dept = classify_with_llm(text, cache=llm_cache)

        success = upsert_to_dept(dept, text, {
            "category": f"llm_{category}" if category else "llm_unknown",
            "original_source": payload.get("source", ""),
            "original_link": payload.get("original_link", ""),
            "llm_classified": True,
        })
        if success:
            synced.add(ch)
            state.setdefault("synced_hashes", []).append(ch)
            stats["synced"][dept] = stats["synced"].get(dept, 0) + 1
        stats["total"] += 1

    save_llm_cache(llm_cache)

    total_synced = sum(stats["synced"].values())
    print(f"  unknown投入: {stats['total']}件中{total_synced}件 (スキップ: {stats['skipped']})")
    for dept, count in sorted(stats["synced"].items()):
        print(f"    {dept}: {count}件")
    return stats


def run_cmem_feed(state: dict, max_batches: int = 10) -> dict:
    """cmem_bridgeデータを部門RAGに自動分類投入.

    Args:
        state: 状態辞書 (synced_hashes管理)
        max_batches: 最大バッチ数 (200件/バッチ)

    Returns:
        stats dict
    """
    print("--- cmem_bridge → 部門RAG ---")
    synced = set(state.get("synced_hashes", []))
    stats = {"total": 0, "synced": {}, "skipped": 0}
    offset = None

    for batch_idx in range(max_batches):
        points, next_offset = fetch_cmem_bridge_batch(offset=offset, limit=CMEM_BATCH_SIZE)
        if not points:
            break

        for p in points:
            payload = p.get("payload", {})
            text = payload.get("memory", payload.get("data", ""))
            if not text or len(text) < 50:
                stats["skipped"] += 1
                continue

            ch = content_hash(text)
            if ch in synced:
                stats["skipped"] += 1
                continue

            cmem_type = payload.get("type", "discovery")
            dept = classify_cmem_to_dept(text, cmem_type)

            success = upsert_to_dept(dept, text, {
                "category": f"cmem_{cmem_type}",
                "original_source": "cmem_bridge",
                "cmem_id": payload.get("cmem_id"),
                "cmem_type": cmem_type,
                "project": payload.get("project", ""),
                "original_created_at": payload.get("created_at", ""),
            })
            if success:
                synced.add(ch)
                state.setdefault("synced_hashes", []).append(ch)
                stats["synced"][dept] = stats["synced"].get(dept, 0) + 1
            stats["total"] += 1

        if next_offset is None:
            break
        offset = next_offset

    total_synced = sum(stats["synced"].values())
    print(f"  cmem投入: {stats['total']}件中{total_synced}件 (スキップ: {stats['skipped']})")
    for dept, count in sorted(stats["synced"].items()):
        print(f"    {dept}: {count}件")
    return stats


def run_feed() -> dict:
    """メイン: mem0_shared→部門RAGのフィード投入."""
    print("=== Department Feed Bridge ===")
    state = load_state()
    stats = {"total": 0, "filtered": 0, "synced": {}}

    # 1. カテゴリ別にmem0_sharedからデータ取得 (x-feed-collector由来)
    for category, dept in CATEGORY_TO_DEPT.items():
        points = fetch_recent_from_shared(category_filter=category, limit=30)
        filtered = filter_quality(points, state)

        synced_count = 0
        for item in filtered:
            success = upsert_to_dept(dept, item["text"], {
                "category": item["category"],
                "original_source": item["source"],
                "link": item.get("link", ""),
            })
            if success:
                state.setdefault("synced_hashes", []).append(item["hash"])
                synced_count += 1

        if synced_count > 0:
            stats["synced"][dept] = stats["synced"].get(dept, 0) + synced_count
            print(f"  {category} → {dept}: {synced_count}件投入")

        stats["total"] += len(points)
        stats["filtered"] += len(filtered)

    # 1.5. cmem_bridgeデータ (Claude Code会話記憶) を部門RAGに投入
    cmem_stats = run_cmem_feed(state, max_batches=10)
    for dept, count in cmem_stats["synced"].items():
        stats["synced"][dept] = stats["synced"].get(dept, 0) + count
    stats["total"] += cmem_stats["total"]

    # 1.7. x-feed-collectorのunknownカテゴリをLLM判定で投入
    unknown_stats = run_unknown_category_feed(state)
    for dept, count in unknown_stats["synced"].items():
        stats["synced"][dept] = stats["synced"].get(dept, 0) + count
    stats["total"] += unknown_stats["total"]

    # 2. 承認済みリクエストの定期実行
    requests = load_approved_requests()
    for req in requests:
        if req.get("enabled", True):
            print(f"  [Request] {req['name']}: {req.get('query', '')}")
            # x-feed-collectorのconfig.yamlに追加済みの場合はスキップ
            # 将来的にはここでカスタム検索を実行

    # ハッシュリストは最新1000件に制限
    state["synced_hashes"] = state["synced_hashes"][-1000:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["stats"] = stats
    save_state(state)

    total_synced = sum(stats["synced"].values())
    print(f"\n完了: {stats['total']}件取得 → {stats['filtered']}件フィルタ通過 → {total_synced}件投入")

    if total_synced > 0:
        try:
            subprocess.run(
                [sys.executable, str(WEBHOOK_SCRIPT),
                 f"📡 Dept Feed: {total_synced}件を部門RAGに投入 {stats['synced']}"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            pass

    return stats


def show_requests():
    requests = load_approved_requests()
    if not requests:
        print("承認済みリクエストなし。")
        print("\nリクエスト追加方法:")
        print('  python dept_feed_bridge.py add-request "リクエスト名" "検索クエリ" "dept_research"')
        return
    print("=== 承認済み情報収集リクエスト ===")
    for r in requests:
        status = "✅有効" if r.get("enabled", True) else "⏸️停止"
        print(f"  [{status}] {r['name']}: query='{r.get('query','')}' → {r.get('target_dept','')}")
        print(f"    申請元: {r.get('requesting_dept','?')} | 承認日: {r.get('approved_at','?')}")


def add_request(name: str, query: str, target_dept: str, requesting_dept: str = "management"):
    """承認済みリクエストを追加."""
    requests = load_approved_requests()
    requests.append({
        "name": name,
        "query": query,
        "target_dept": target_dept,
        "requesting_dept": requesting_dept,
        "enabled": True,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "source": "x-feed-collector",
    })
    save_approved_requests(requests)
    print(f"リクエスト追加: {name} (query='{query}' → {target_dept})")


def show_status():
    state = load_state()
    print(f"最終実行: {state.get('last_run', 'なし')}")
    print(f"同期済みハッシュ: {len(state.get('synced_hashes', []))}件")
    stats = state.get("stats", {})
    if stats.get("synced"):
        print(f"前回投入: {stats['synced']}")

    # 部門コレクション状態
    for dept in ["dept_hr", "dept_research", "dept_design", "dept_build", "dept_qa"]:
        try:
            info = qdrant_request(f"/collections/{dept}", method="GET")
            count = info.get("result", {}).get("points_count", "?")
            print(f"  {dept}: {count} ポイント")
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "requests":
        show_requests()
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif len(sys.argv) > 4 and sys.argv[1] == "add-request":
        add_request(sys.argv[2], sys.argv[3], sys.argv[4],
                    sys.argv[5] if len(sys.argv) > 5 else "management")
    else:
        run_feed()
