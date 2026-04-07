"""軽量パーソナルアシスタント デーモン.

タスクスケジューラから5分間隔で起動され、以下を実行:
1. タスクキューからgemma4向けタスクを処理
2. Sonnet CLIへの委譲判断
3. スケジュール・リマインダー確認
4. HW/X監視結果の要約
5. Discord Webhookで結果通知

使い方:
    python scripts/assistant_daemon.py              # 通常実行
    python scripts/assistant_daemon.py --dry-run    # 実行せず表示のみ
    python scripts/assistant_daemon.py status       # ステータス表示
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# src/ を import path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from assistant_queue import TaskQueue, Task
from supervisor import write_heartbeat, read_heartbeat, heartbeat_age_min

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [assistant] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

DAEMON_DIR = Path.home() / ".helix-agent" / "assistant"
STATE_FILE = DAEMON_DIR / "daemon_state.json"
LOCK_FILE = DAEMON_DIR / "daemon.lock"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# Sonnet CLI呼び出し設定
SONNET_TIMEOUT = 120  # 秒
GEMMA4_TIMEOUT = 60   # 秒

# Ollama設定
OLLAMA_URL = "http://localhost:11434"


# ---------------------------------------------------------------------------
# ロック（多重起動防止）
# ---------------------------------------------------------------------------

def acquire_daemon_lock() -> bool:
    """デーモンロックを取得. 既に実行中なら False."""
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            lock_data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            lock_time = datetime.fromisoformat(lock_data["timestamp"])
            age_min = (datetime.now(timezone.utc) - lock_time).total_seconds() / 60
            if age_min < 10:  # 10分以内のロックは有効
                log.info("別のデーモンが実行中 (PID: %s, %d分前)", lock_data.get("pid"), int(age_min))
                return False
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    LOCK_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    return True


def release_daemon_lock() -> None:
    """デーモンロックを解放."""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 状態管理
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "last_run": None,
        "total_runs": 0,
        "tasks_processed": {"gemma4": 0, "sonnet": 0, "escalated": 0},
        "last_briefing": None,
        "errors": [],
    }


def save_state(state: dict) -> None:
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# gemma4 処理
# ---------------------------------------------------------------------------

async def process_with_gemma4(task: Task) -> str | None:
    """gemma4 (Ollama) でタスクを処理."""
    try:
        from ollama_client import OllamaClient
    except ImportError:
        log.warning("ollama_client が見つかりません")
        return None

    client = OllamaClient(timeout=GEMMA4_TIMEOUT)
    if not await client.is_available():
        log.warning("Ollama が起動していません")
        return None

    # 軽量モデル選択（常時運用はVRAM節約優先）
    model_config_file = Path.home() / ".helix-agent" / "assistant" / "model_config.json"
    model = "gemma4:e4b"  # デフォルト
    if model_config_file.exists():
        try:
            mc = json.loads(model_config_file.read_text(encoding="utf-8"))
            model = mc.get("daemon_model", "gemma4:e4b")
        except (json.JSONDecodeError, OSError):
            pass
    # モデルが利用可能か確認
    available = {m["name"] for m in await client.list_models()}
    if model not in available:
        # フォールバック: e4b → e2b
        for fallback in ["gemma4:e4b", "gemma4:e2b"]:
            if fallback in available:
                model = fallback
                break
        else:
            log.warning("利用可能な軽量モデルがありません。")
            return None

    log.info("gemma4処理: %s (model=%s)", task.description[:50], model)

    prompt = f"""以下のタスクを処理してください。簡潔に結果を返してください。

タスク: {task.description}
カテゴリ: {task.category}
"""
    if task.payload:
        prompt += f"\n追加情報: {json.dumps(task.payload, ensure_ascii=False)}"

    try:
        response = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": "あなたは軽量パーソナルアシスタントです。簡潔に回答してください。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            num_ctx=4096,
        )
        return response.strip() if response else None
    except Exception as e:
        log.error("gemma4処理エラー: %s", e)
        return None


# ---------------------------------------------------------------------------
# Sonnet CLI 処理
# ---------------------------------------------------------------------------

def process_with_sonnet(task: Task) -> str | None:
    """Claude Code CLI (Sonnet) でタスクを処理."""
    prompt = task.description
    if task.payload:
        prompt += f"\n\n追加情報: {json.dumps(task.payload, ensure_ascii=False)}"

    try:
        result = subprocess.run(
            ["claude", "--model", "sonnet", "-p", prompt, "--no-input"],
            capture_output=True, text=True, timeout=SONNET_TIMEOUT,
            cwd=str(Path.home()),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        else:
            log.warning("Sonnet CLI失敗: returncode=%d, stderr=%s",
                       result.returncode, result.stderr[:200] if result.stderr else "")
            return None
    except subprocess.TimeoutExpired:
        log.warning("Sonnet CLIタイムアウト (%ds)", SONNET_TIMEOUT)
        return None
    except FileNotFoundError:
        log.warning("claude CLI が見つかりません")
        return None
    except Exception as e:
        log.error("Sonnet CLI エラー: %s", e)
        return None


# ---------------------------------------------------------------------------
# 定期チェック（HW/X/スケジュール）
# ---------------------------------------------------------------------------

def check_hw_alerts() -> list[str]:
    """HWモニターのアラートを確認."""
    hw_file = Path.home() / ".helix-agent" / "hw_monitor" / "hw_status.json"
    if not hw_file.exists():
        return []
    try:
        status = json.loads(hw_file.read_text(encoding="utf-8"))
        alerts = status.get("alerts", [])
        return [a.get("message", "") for a in alerts if a.get("level") in ("WARNING", "CRITICAL")]
    except (json.JSONDecodeError, OSError):
        return []


def check_x_monitor_updates() -> str | None:
    """X監視の最新結果を確認."""
    x_dir = Path.home() / ".helix-agent" / "x_monitor"
    if not x_dir.exists():
        return None
    # 最新ファイルを探す
    files = sorted(x_dir.glob("x_monitor_*.json"), reverse=True)
    if not files:
        return None
    latest = files[0]
    try:
        mtime = latest.stat().st_mtime
        age_min = (time.time() - mtime) / 60
        if age_min > 60:  # 1時間以上前のデータは古い
            return None
        data = json.loads(latest.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            high_score = [e for e in data if e.get("relevance_score", 0) >= 8]
            if high_score:
                titles = [e.get("title", "?")[:40] for e in high_score[:3]]
                return f"X高スコア{len(high_score)}件: " + " / ".join(titles)
    except (json.JSONDecodeError, OSError):
        pass
    return None


def generate_briefing() -> str | None:
    """朝のブリーフィングを生成（7:00-9:00の間に1回）."""
    now = datetime.now()
    if not (7 <= now.hour <= 9):
        return None

    state = load_state()
    last_briefing = state.get("last_briefing")
    if last_briefing:
        try:
            last_dt = datetime.fromisoformat(last_briefing)
            if (now - last_dt.replace(tzinfo=None)).total_seconds() < 12 * 3600:
                return None  # 12時間以内にブリーフィング済み
        except (ValueError, TypeError):
            pass

    parts = ["**朝のブリーフィング**"]

    # HW状態
    hw_alerts = check_hw_alerts()
    if hw_alerts:
        parts.append(f"HW: {', '.join(hw_alerts)}")
    else:
        parts.append("HW: 正常")

    # X監視
    x_update = check_x_monitor_updates()
    if x_update:
        parts.append(f"X: {x_update}")

    # キュー状態
    q = TaskQueue()
    stats = q.stats()
    if stats["pending"] > 0:
        parts.append(f"キュー: 待機{stats['pending']}件 (gemma4:{stats['by_target']['gemma4']} / sonnet:{stats['by_target']['sonnet']} / opus:{stats['by_target']['opus']})")

    state["last_briefing"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Discord通知
# ---------------------------------------------------------------------------

def notify_discord(message: str) -> bool:
    """Discord Webhookで通知."""
    if not WEBHOOK_SCRIPT.exists():
        log.warning("Webhookスクリプトが見つかりません: %s", WEBHOOK_SCRIPT)
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(WEBHOOK_SCRIPT), message],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        log.error("Discord通知エラー: %s", e)
        return False


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

async def run(dry_run: bool = False) -> dict:
    """メイン処理. 戻り値は実行結果サマリ."""
    summary = {"gemma4_processed": 0, "sonnet_processed": 0, "escalated": 0, "notifications": []}
    q = TaskQueue()

    # ハートビート送信
    write_heartbeat("assistant_daemon", {"queue_size": q.stats()["pending"]})

    # 1. 古いタスクのクリーンアップ
    q.cleanup(max_age_hours=48)

    # 2. gemma4向けタスクを処理
    gemma4_tasks = q.get_pending(target="gemma4")
    for task in gemma4_tasks[:5]:  # 1回のrunで最大5件
        log.info("処理中 [gemma4]: %s", task.description[:60])
        if dry_run:
            log.info("  (dry-run: スキップ)")
            continue

        q.update_status(task.id, "in_progress")
        result = await process_with_gemma4(task)

        if result:
            q.complete(task.id, result)
            summary["gemma4_processed"] += 1
            log.info("  完了: %s", result[:80])
        else:
            # gemma4で失敗 → sonnetにエスカレート
            q.escalate(task.id, "sonnet", reason="gemma4処理失敗")
            summary["escalated"] += 1
            log.info("  → sonnetにエスカレート")

    # 3. sonnet向けタスクを処理
    sonnet_tasks = q.get_pending(target="sonnet")
    for task in sonnet_tasks[:3]:  # 1回のrunで最大3件
        log.info("処理中 [sonnet]: %s", task.description[:60])
        if dry_run:
            log.info("  (dry-run: スキップ)")
            continue

        q.update_status(task.id, "in_progress")
        result = process_with_sonnet(task)

        if result:
            q.complete(task.id, result)
            summary["sonnet_processed"] += 1
            log.info("  完了: %s", result[:80])
        else:
            # sonnetでも失敗 → opusにエスカレート
            q.escalate(task.id, "opus", reason="sonnet処理失敗")
            summary["escalated"] += 1
            log.info("  → opusにエスカレート")

    # 4. 定期チェック
    hw_alerts = check_hw_alerts()
    if hw_alerts:
        msg = "⚠️ **HWアラート**: " + " / ".join(hw_alerts)
        summary["notifications"].append(msg)

    # 5. ブリーフィング
    briefing = generate_briefing()
    if briefing:
        summary["notifications"].append(briefing)

    # 6. 通知送信
    if not dry_run:
        for msg in summary["notifications"]:
            notify_discord(msg)

    # 7. 状態更新
    state = load_state()
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["total_runs"] = state.get("total_runs", 0) + 1
    state["tasks_processed"]["gemma4"] = state["tasks_processed"].get("gemma4", 0) + summary["gemma4_processed"]
    state["tasks_processed"]["sonnet"] = state["tasks_processed"].get("sonnet", 0) + summary["sonnet_processed"]
    state["tasks_processed"]["escalated"] = state["tasks_processed"].get("escalated", 0) + summary["escalated"]
    save_state(state)

    return summary


def show_status():
    """ステータス表示."""
    state = load_state()
    q = TaskQueue()
    stats = q.stats()

    print("=== Assistant Daemon ===")
    print(f"  最終実行: {state.get('last_run', 'なし')}")
    print(f"  総実行回数: {state.get('total_runs', 0)}")
    print(f"  処理済み: gemma4={state['tasks_processed'].get('gemma4', 0)} / "
          f"sonnet={state['tasks_processed'].get('sonnet', 0)} / "
          f"escalated={state['tasks_processed'].get('escalated', 0)}")
    print(f"\n=== タスクキュー ===")
    print(f"  計{stats['total']}件: 待機{stats['pending']} / 処理中{stats['in_progress']} / 完了{stats['completed']}")
    print(f"  gemma4:{stats['by_target']['gemma4']} / sonnet:{stats['by_target']['sonnet']} / opus:{stats['by_target']['opus']}")

    pending = q.get_pending()
    if pending:
        print(f"\n  待機中タスク:")
        for t in pending[:10]:
            print(f"    [{t.priority}] ({t.target}) {t.description[:60]}")


def main():
    parser = argparse.ArgumentParser(description="軽量パーソナルアシスタント デーモン")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "status"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.command == "status":
        show_status()
        return

    # ロック取得
    if not acquire_daemon_lock():
        log.info("別のデーモンが実行中。終了します。")
        return

    try:
        summary = asyncio.run(run(dry_run=args.dry_run))
        total = summary["gemma4_processed"] + summary["sonnet_processed"]
        log.info("完了: %d件処理 (gemma4:%d / sonnet:%d / エスカレート:%d / 通知:%d)",
                total, summary["gemma4_processed"], summary["sonnet_processed"],
                summary["escalated"], len(summary["notifications"]))
    finally:
        release_daemon_lock()


if __name__ == "__main__":
    main()
