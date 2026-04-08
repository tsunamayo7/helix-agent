"""Claude使用量モニター — リセット期間までの消費ペースを監視.

ローカルJSONLセッションファイルからトークン使用量を集計し、
リミット到達予測と警告をDiscord Webhookで通知。

タスクスケジューラから15分間隔で実行。

データソース:
  1. ~/.claude/projects/*/  内のセッションJSONL (usage.input_tokens, output_tokens)
  2. claude-monitor (ccusage) がインストール済みなら補完

使い方:
    python scripts/usage_monitor.py              # チェック実行
    python scripts/usage_monitor.py status        # 現在の使用状況表示
    python scripts/usage_monitor.py reset-info    # リセット時刻情報
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
MONITOR_DIR = Path.home() / ".helix-agent" / "usage_monitor"
STATE_FILE = MONITOR_DIR / "state.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# Max 20x プラン設定
PLAN = "max20"
RESET_HOUR_JST = 9  # リセット時刻 (JST)
# Max 20x は通常のPro 5倍の20倍 = 推定トークン上限
# 公式には「20x the usage of Pro」とだけ記載
# Pro: ~45M tokens/5h window → Max20x: ~900M tokens/5h window (推定)
# 安全マージンを見て80%で警告
WARN_THRESHOLD_PCT = 80
CRITICAL_THRESHOLD_PCT = 95

# JST timezone
JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# トークン使用量の集計
# ---------------------------------------------------------------------------

def get_current_period_start() -> datetime:
    """現在のリセット期間の開始時刻を取得 (JST 9:00基準)."""
    now = datetime.now(JST)
    today_reset = now.replace(hour=RESET_HOUR_JST, minute=0, second=0, microsecond=0)
    if now >= today_reset:
        return today_reset
    return today_reset - timedelta(days=1)


def get_next_reset() -> datetime:
    """次のリセット時刻を取得."""
    return get_current_period_start() + timedelta(days=1)


def count_tokens_in_period(period_start: datetime) -> dict:
    """指定期間以降のトークン使用量を集計.

    セッションJSONLファイルから usage.input_tokens, output_tokens を合算。
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "sessions_counted": 0,
        "messages_counted": 0,
    }

    # UTC変換してISO比較
    period_start_utc = period_start.astimezone(timezone.utc)
    period_start_ts = period_start_utc.strftime("%Y-%m-%dT%H:%M:%S")

    # 全プロジェクトのJSONLファイルを走査
    for jsonl_path in PROJECTS_DIR.rglob("*.jsonl"):
        # ファイルの更新日時で粗くフィルタ
        try:
            mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=JST)
            if mtime < period_start:
                continue
        except OSError:
            continue

        session_counted = False
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # タイムスタンプフィルタ
                    ts = entry.get("timestamp", "")
                    if ts and ts < period_start_ts:
                        continue

                    # usage情報を抽出
                    msg = entry.get("message", {})
                    usage = msg.get("usage", {})
                    if not usage:
                        continue

                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cache_create = usage.get("cache_creation_input_tokens", 0)
                    cache_read = usage.get("cache_read_input_tokens", 0)

                    if inp or out:
                        totals["input_tokens"] += inp
                        totals["output_tokens"] += out
                        totals["cache_creation_tokens"] += cache_create
                        totals["cache_read_tokens"] += cache_read
                        totals["messages_counted"] += 1
                        if not session_counted:
                            totals["sessions_counted"] += 1
                            session_counted = True

        except (OSError, PermissionError):
            continue

    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return totals


# ---------------------------------------------------------------------------
# 消費ペース予測
# ---------------------------------------------------------------------------

def predict_exhaustion(totals: dict, period_start: datetime) -> dict | None:
    """現在のペースでリミットに到達する時刻を予測."""
    now = datetime.now(JST)
    elapsed_hours = max(0.01, (now - period_start).total_seconds() / 3600)
    remaining_hours = max(0, (get_next_reset() - now).total_seconds() / 3600)

    total = totals["total_tokens"]
    if total == 0:
        return None

    rate_per_hour = total / elapsed_hours

    return {
        "tokens_per_hour": int(rate_per_hour),
        "elapsed_hours": round(elapsed_hours, 1),
        "remaining_hours": round(remaining_hours, 1),
        "projected_total_at_reset": int(rate_per_hour * (elapsed_hours + remaining_hours)),
    }


# ---------------------------------------------------------------------------
# ブラウザ経由の使用率取得（オプション）
# ---------------------------------------------------------------------------

def get_usage_from_browser() -> dict | None:
    """claude.ai/settings/usage ページから使用率を取得（agent-browser CDP経由）.

    15分に1回程度の実行を想定。Chrome CDPポートが必要。
    """
    usage_file = Path.home() / ".helix-agent" / "claude_usage" / "latest.json"
    try:
        # claude_usage_scraper.py を呼び出し
        scraper = Path(__file__).resolve().parent / "claude_usage_scraper.py"
        if not scraper.exists():
            return None
        result = subprocess.run(
            [sys.executable, str(scraper), "fetch", "--json"],
            capture_output=True, text=True, timeout=30, encoding="utf-8",
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    # フォールバック: 最新ファイルがあればそれを読む
    if usage_file.exists():
        try:
            data = json.loads(usage_file.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return None


# ---------------------------------------------------------------------------
# 状態管理
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_check": None, "last_alert": None, "alert_level": None}


def save_state(state: dict) -> None:
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Discord通知
# ---------------------------------------------------------------------------

def notify(message: str) -> bool:
    if not WEBHOOK_SCRIPT.exists():
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(WEBHOOK_SCRIPT), message],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def should_alert(state: dict, level: str) -> bool:
    """同一レベルのアラートは1時間に1回まで."""
    if state.get("alert_level") != level:
        return True
    last = state.get("last_alert")
    if not last:
        return True
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        return elapsed > 3600
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# ハートビート
# ---------------------------------------------------------------------------

def write_heartbeat():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from supervisor import write_heartbeat as _wb
        _wb("usage_monitor")
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def run_check() -> dict:
    """使用量チェックを実行."""
    write_heartbeat()
    state = load_state()
    now = datetime.now(JST)

    period_start = get_current_period_start()
    next_reset = get_next_reset()
    totals = count_tokens_in_period(period_start)
    prediction = predict_exhaustion(totals, period_start)

    result = {
        "timestamp": now.isoformat(),
        "period_start": period_start.isoformat(),
        "next_reset": next_reset.isoformat(),
        "totals": totals,
        "prediction": prediction,
        "alert": None,
    }

    # 保存
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    state["totals"] = totals

    # アラート判定（トークン数ベースの推定）
    # Max 20x の正確な上限は非公開だが、ペースで判断
    if prediction and totals["total_tokens"] > 0:
        hours_left = prediction["remaining_hours"]
        rate = prediction["tokens_per_hour"]

        # 消費が加速している場合の警告
        if hours_left > 0 and hours_left < 2 and rate > 500_000:
            alert_msg = (
                f"**Claude使用量警告**: リセットまで{hours_left:.1f}時間\n"
                f"現在のペース: {rate:,} tok/h\n"
                f"今期間合計: {totals['total_tokens']:,} tokens "
                f"({totals['sessions_counted']}セッション, {totals['messages_counted']}メッセージ)\n"
                f"次回リセット: {next_reset.strftime('%m/%d %H:%M')} JST"
            )
            if should_alert(state, "warn"):
                notify(alert_msg)
                state["alert_level"] = "warn"
                state["last_alert"] = datetime.now(timezone.utc).isoformat()
            result["alert"] = "warn"

    # ブラウザ使用率取得（CDP接続可能な場合のみ）
    browser_usage = get_usage_from_browser()
    # 鮮度チェック: 1時間以上古いデータは無視
    if browser_usage:
        try:
            data_ts = datetime.fromisoformat(browser_usage.get("timestamp", ""))
            age_hours = (now - data_ts).total_seconds() / 3600
            if age_hours > 1.0:
                browser_usage["stale"] = True
                browser_usage["age_hours"] = round(age_hours, 1)
        except (ValueError, TypeError):
            pass
    if browser_usage:
        result["browser_usage"] = browser_usage
        # ブラウザ使用率ベースのアラート
        weekly_pct = browser_usage.get("weekly_all", {}).get("percent", 0)
        sonnet_pct = browser_usage.get("sonnet_only", {}).get("percent", 0)
        if weekly_pct >= 85 or sonnet_pct >= 85:
            level = "critical_browser"
            alert_msg = (
                f"**Claude使用率 CRITICAL**\n"
                f"- 週間(全モデル): {weekly_pct}%\n"
                f"- Sonnetのみ: {sonnet_pct}%\n"
                f"リセット: {browser_usage.get('weekly_all', {}).get('reset', '?')}"
            )
            if should_alert(state, level):
                notify(alert_msg)
                state["alert_level"] = level
                state["last_alert"] = datetime.now(timezone.utc).isoformat()
        elif weekly_pct >= 70 or sonnet_pct >= 70:
            level = "warn_browser"
            alert_msg = (
                f"**Claude使用率 WARNING**\n"
                f"- 週間(全モデル): {weekly_pct}%\n"
                f"- Sonnetのみ: {sonnet_pct}%\n"
                f"リセット: {browser_usage.get('weekly_all', {}).get('reset', '?')}"
            )
            if should_alert(state, level):
                notify(alert_msg)
                state["alert_level"] = level
                state["last_alert"] = datetime.now(timezone.utc).isoformat()

    save_state(state)

    # 監視データをファイルにも保存（他のデーモンから参照用）
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    (MONITOR_DIR / "latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return result


def show_status():
    """現在の使用状況を表示."""
    now = datetime.now(JST)
    period_start = get_current_period_start()
    next_reset = get_next_reset()
    totals = count_tokens_in_period(period_start)
    prediction = predict_exhaustion(totals, period_start)

    hours_elapsed = (now - period_start).total_seconds() / 3600
    hours_remaining = (next_reset - now).total_seconds() / 3600

    print(f"=== Claude Usage Monitor (Max 20x) ===")
    print(f"  現在: {now.strftime('%Y-%m-%d %H:%M')} JST")
    print(f"  期間: {period_start.strftime('%m/%d %H:%M')} ~ {next_reset.strftime('%m/%d %H:%M')} JST")
    print(f"  経過: {hours_elapsed:.1f}h / 残り: {hours_remaining:.1f}h")
    print()
    print(f"=== トークン使用量 (今期間) ===")
    print(f"  入力: {totals['input_tokens']:,}")
    print(f"  出力: {totals['output_tokens']:,}")
    print(f"  合計: {totals['total_tokens']:,}")
    print(f"  キャッシュ生成: {totals['cache_creation_tokens']:,}")
    print(f"  キャッシュ読取: {totals['cache_read_tokens']:,}")
    print(f"  セッション数: {totals['sessions_counted']}")
    print(f"  メッセージ数: {totals['messages_counted']}")

    if prediction:
        print()
        print(f"=== 消費ペース予測 ===")
        print(f"  現在のペース: {prediction['tokens_per_hour']:,} tok/h")
        print(f"  リセットまでの予測消費: {prediction['projected_total_at_reset']:,} tokens")

    # ブラウザ使用率（claude.ai公式データ）
    usage_file = Path.home() / ".helix-agent" / "claude_usage" / "latest.json"
    if usage_file.exists():
        try:
            browser_data = json.loads(usage_file.read_text(encoding="utf-8"))
            ts = browser_data.get("timestamp", "?")
            weekly = browser_data.get("weekly_all", {})
            sonnet = browser_data.get("sonnet_only", {})
            session = browser_data.get("session", {})
            print()
            print(f"=== 公式使用率 (claude.ai, {ts}) ===")
            if session.get("percent") is not None:
                print(f"  セッション:    {session['percent']}%  (リセット: {session.get('reset', '?')})")
            print(f"  週間(全モデル): {weekly.get('percent', '?')}%  (リセット: {weekly.get('reset', '?')})")
            print(f"  Sonnetのみ:    {sonnet.get('percent', '?')}%  (リセット: {sonnet.get('reset', '?')})")
        except (json.JSONDecodeError, OSError):
            pass


def show_reset_info():
    """リセット時刻情報."""
    now = datetime.now(JST)
    next_reset = get_next_reset()
    remaining = next_reset - now
    print(f"次回リセット: {next_reset.strftime('%Y-%m-%d %H:%M')} JST")
    print(f"残り: {remaining.total_seconds() / 3600:.1f}時間")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif len(sys.argv) > 1 and sys.argv[1] == "reset-info":
        show_reset_info()
    else:
        result = run_check()
        total = result["totals"]["total_tokens"]
        msgs = result["totals"]["messages_counted"]
        print(f"チェック完了: {total:,} tokens ({msgs}メッセージ)")
        if result["alert"]:
            print(f"  アラート: {result['alert']}")
