"""Department Dataset Builder — 部門RAGからFT用JSONL教師データを生成.

dept_ft_advisor.py で判定された「READY」状態の部門について、
RAG内のデータを instruction-output pair に変換し、ShareGPT形式で出力する。

フロー:
  1. 部門RAGから全ポイント取得
  2. 各ポイントを元に gemma4:31b で Q&A ペアを自動生成
  3. 重複排除 (question_hash)
  4. 品質フィルタ (最小文字数、言語チェック)
  5. train/validate 8:2 分割
  6. JSONL 出力 (~/.helix-agent/ft_datasets/<dept>/{train,valid}.jsonl)

使い方:
    python scripts/dept_dataset_builder.py dept_qa        # 特定部門
    python scripts/dept_dataset_builder.py --all          # 全部門 (READY判定済のみ)
    python scripts/dept_dataset_builder.py --export       # 結果をmemoryにサマリー保存
    python scripts/dept_dataset_builder.py status         # 既存データセットの状態
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

QDRANT_URL = "http://localhost:6333"
OLLAMA_URL = "http://localhost:11434"
QA_MODEL = "gemma4:31b"
STATE_DIR = Path.home() / ".helix-agent" / "ft_datasets"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"

# 部門ごとのFT準備最小ポイント数 (dept_ft_advisor.pyと同じ値)
DEPT_MIN_POINTS = {
    "dept_hr": 500,
    "dept_research": 1000,
    "dept_design": 800,
    "dept_build": 800,
    "dept_qa": 600,
}

# 部門ごとの質問生成スタイル
DEPT_QA_STYLES = {
    "dept_hr": {
        "system_prompt": "あなたは人事・採用の専門家です。",
        "qa_instruction": (
            "以下のテキストから、採用・キャリアに関する実践的な質問とその回答を1ペア生成してください。"
            "質問は応募者/人事担当者の視点で、回答は市場価値・適合性を重視した実用的な内容にしてください。"
        ),
    },
    "dept_research": {
        "system_prompt": "あなたは技術調査の専門家です。",
        "qa_instruction": (
            "以下のテキストから、技術調査に関する質問とその網羅的な回答を1ペア生成してください。"
            "質問は技術者の視点で、回答は最新性と網羅性を重視した実用的な内容にしてください。"
        ),
    },
    "dept_design": {
        "system_prompt": "あなたはソフトウェアアーキテクトです。",
        "qa_instruction": (
            "以下のテキストから、システム設計・アーキテクチャに関する質問とその回答を1ペア生成してください。"
            "質問は設計者の視点で、回答は拡張性・保守性を重視した具体的な内容にしてください。"
        ),
    },
    "dept_build": {
        "system_prompt": "あなたはシニア実装エンジニアです。",
        "qa_instruction": (
            "以下のテキストから、実装・デバッグに関する質問とその回答を1ペア生成してください。"
            "質問は開発者の視点で、回答は品質・テスト通過を重視した具体的なコード例を含めてください。"
        ),
    },
    "dept_qa": {
        "system_prompt": "あなたは品質保証・セキュリティの専門家です。",
        "qa_instruction": (
            "以下のテキストから、品質管理・セキュリティに関する質問とその回答を1ペア生成してください。"
            "質問は防御的な視点で、回答は最悪ケース・OWASP Top 10・テスト設計を含めてください。"
        ),
    },
}

# 品質フィルタ
MIN_QUESTION_LEN = 10
MIN_ANSWER_LEN = 30
MAX_ANSWER_LEN = 2000


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def qdrant_get_collection(name: str) -> dict:
    try:
        with urllib.request.urlopen(f"{QDRANT_URL}/collections/{name}", timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def qdrant_scroll(collection: str, limit: int = 1000) -> list[dict]:
    """コレクションから全ポイント取得 (ページング)."""
    points = []
    offset = None
    batch = 200

    while len(points) < limit:
        body = {
            "limit": batch,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset

        try:
            req = urllib.request.Request(
                f"{QDRANT_URL}/collections/{collection}/points/scroll",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"  scroll error: {e}")
            break

        r = data.get("result", {})
        batch_points = r.get("points", [])
        if not batch_points:
            break
        points.extend(batch_points)

        offset = r.get("next_page_offset")
        if offset is None:
            break

    return points[:limit]


def ollama_generate(prompt: str, timeout: int = 180) -> str:
    try:
        data = json.dumps({
            "model": QA_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,  # thinking無効化 (gemma4のhidden thinking対策)
            "options": {
                "temperature": 0.2,
                "num_predict": 2048,  # 十分な出力長 (thinking + answer両方収まる)
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read().decode("utf-8"))
        return result.get("response", "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Q&A生成
# ---------------------------------------------------------------------------


def generate_qa_pair(text: str, dept: str) -> dict | None:
    """gemma4で1テキストから1つのQ&Aペアを生成."""
    style = DEPT_QA_STYLES.get(dept)
    if not style:
        return None

    prompt = (
        f"{style['system_prompt']}\n\n"
        f"{style['qa_instruction']}\n\n"
        f"テキスト:\n{text[:1500]}\n\n"
        "以下のJSON形式で回答してください:\n"
        '{"question": "質問文", "answer": "回答文"}\n'
        "他の文字は含めないでください。"
    )

    response = ollama_generate(prompt, timeout=180)
    if not response:
        return None

    # JSON抽出 (markdownコードブロック対応)
    try:
        # ```json ... ``` を除去
        import re
        cleaned = re.sub(r'```(?:json)?\s*', '', response)
        cleaned = re.sub(r'```', '', cleaned)
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        qa = json.loads(cleaned[start:end])
        question = str(qa.get("question", "")).strip()
        answer = str(qa.get("answer", "")).strip()

        # 品質フィルタ
        if len(question) < MIN_QUESTION_LEN:
            return None
        if len(answer) < MIN_ANSWER_LEN or len(answer) > MAX_ANSWER_LEN:
            return None

        return {"question": question, "answer": answer}
    except Exception:
        return None


def build_dept_dataset(dept: str, max_points: int = 500, verbose: bool = True) -> dict:
    """1部門のデータセットを生成."""
    info = qdrant_get_collection(dept)
    points_count = info.get("result", {}).get("points_count", 0) if info else 0

    min_required = DEPT_MIN_POINTS.get(dept, 500)

    if verbose:
        print(f"\n=== {dept} ===")
        print(f"  RAGポイント: {points_count} / {min_required}")

    if points_count < min_required:
        if verbose:
            print(f"  SKIP: データ不足 (不足: {min_required - points_count})")
        return {
            "dept": dept,
            "status": "insufficient",
            "points_count": points_count,
            "pairs_generated": 0,
        }

    # データ取得
    if verbose:
        print(f"  取得中... (max {max_points}点)")
    points = qdrant_scroll(dept, limit=max_points)
    if verbose:
        print(f"  取得完了: {len(points)}点")

    # Q&A生成
    pairs: list[dict] = []
    seen_questions: set = set()
    skipped = 0

    for idx, p in enumerate(points):
        payload = p.get("payload", {})
        text = payload.get("data", payload.get("memory", ""))
        if not text or len(text) < 100:
            skipped += 1
            continue

        if verbose and idx % 20 == 0:
            print(f"    [{idx}/{len(points)}] 生成中... ({len(pairs)}ペア)")

        qa = generate_qa_pair(text, dept)
        if qa is None:
            skipped += 1
            continue

        qh = content_hash(qa["question"])
        if qh in seen_questions:
            skipped += 1
            continue
        seen_questions.add(qh)

        # ShareGPT形式 (convesations形式)
        pairs.append({
            "conversations": [
                {"from": "human", "value": qa["question"]},
                {"from": "gpt", "value": qa["answer"]},
            ],
            "source": "dept_rag",
            "dept": dept,
            "original_hash": content_hash(text),
        })

    if verbose:
        print(f"  生成完了: {len(pairs)}ペア (スキップ: {skipped})")

    # train/valid 8:2 分割
    random.shuffle(pairs)
    split = int(len(pairs) * 0.8)
    train = pairs[:split]
    valid = pairs[split:]

    # 出力
    dept_dir = STATE_DIR / dept
    dept_dir.mkdir(parents=True, exist_ok=True)

    train_path = dept_dir / "train.jsonl"
    valid_path = dept_dir / "valid.jsonl"

    with train_path.open("w", encoding="utf-8") as f:
        for p in train:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with valid_path.open("w", encoding="utf-8") as f:
        for p in valid:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # メタデータ
    meta = {
        "dept": dept,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_points": len(points),
        "pairs_total": len(pairs),
        "pairs_train": len(train),
        "pairs_valid": len(valid),
        "pairs_skipped": skipped,
        "model": QA_MODEL,
    }
    (dept_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    if verbose:
        print(f"  出力: {dept_dir}")
        print(f"    train: {len(train)}ペア")
        print(f"    valid: {len(valid)}ペア")

    return {
        "dept": dept,
        "status": "success",
        "points_count": points_count,
        "pairs_generated": len(pairs),
        "train": len(train),
        "valid": len(valid),
        "path": str(dept_dir),
    }


def show_status():
    """既存データセットの状態を表示."""
    if not STATE_DIR.exists():
        print("データセットディレクトリなし")
        return

    print("=== 既存データセット ===")
    for dept_dir in sorted(STATE_DIR.iterdir()):
        if not dept_dir.is_dir():
            continue
        meta_file = dept_dir / "meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            print(f"\n[{meta['dept']}]")
            print(f"  生成: {meta['generated_at'][:19]}")
            print(f"  train: {meta['pairs_train']}ペア")
            print(f"  valid: {meta['pairs_valid']}ペア")
            print(f"  path: {dept_dir}")
        except Exception as e:
            print(f"  {dept_dir.name}: メタデータ読み込み失敗 ({e})")


def notify_discord(results: list[dict]) -> None:
    success = [r for r in results if r.get("status") == "success"]
    if not success:
        return
    lines = [f"📚 **部門データセット生成完了** ({len(success)}部門)"]
    for r in success:
        lines.append(f"  - {r['dept']}: {r['pairs_generated']}ペア (train {r['train']} / valid {r['valid']})")
    try:
        subprocess.run(
            ["python", str(WEBHOOK_SCRIPT), "\n".join(lines)],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def main():
    verbose = "--quiet" not in sys.argv
    max_points = 500
    if "--max" in sys.argv:
        idx = sys.argv.index("--max")
        if idx + 1 < len(sys.argv):
            try:
                max_points = int(sys.argv[idx + 1])
            except ValueError:
                pass

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
        return

    dept_arg = None
    for a in sys.argv[1:]:
        if a.startswith("dept_"):
            dept_arg = a
            break

    if "--all" in sys.argv:
        target_depts = list(DEPT_MIN_POINTS.keys())
    elif dept_arg:
        target_depts = [dept_arg]
    else:
        print("Usage: python scripts/dept_dataset_builder.py [--all | dept_xxx] [--max N] [status]")
        return

    results = []
    for d in target_depts:
        r = build_dept_dataset(d, max_points=max_points, verbose=verbose)
        results.append(r)

    if verbose:
        print("\n=== サマリー ===")
        for r in results:
            print(f"  {r['dept']}: {r.get('status')}, {r.get('pairs_generated', 0)}ペア")

    notify_discord(results)


if __name__ == "__main__":
    main()
