"""Failover Orchestrator — AIサービスのフォールバック管理.

Claude Code → Sonnet Agent → Codex CLI → Ollama (gemma4)
の優先順位で、稼働中の最高優先度AIを管理者として維持する。

watchdog.pyと連携:
  - watchdog: Claude CLI生存監視 → 停止検出 → crash_recovery
  - orchestrator: 全AIサービスの健全性チェック → フォールバック起動

使い方:
    python scripts/failover_orchestrator.py           # チェック+フォールバック
    python scripts/failover_orchestrator.py status    # 状態表示
    python scripts/failover_orchestrator.py recover   # Claude復帰時の引き継ぎ
"""

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

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

STATE_DIR = Path.home() / ".helix-agent" / "failover"
STATE_FILE = STATE_DIR / "state.json"
FAILOVER_LOG = Path.home() / ".claude" / "projects" / "C--Development" / "memory" / "failover_log.md"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
CHECKPOINT_FILE = MEMORY_DIR / "session_checkpoint.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

QDRANT_URL = "http://localhost:6333"
OLLAMA_URL = "http://localhost:11434"
HEALTH_URL = "http://localhost:8800"

# サービス定義（優先順）
SERVICES = [
    {
        "name": "claude_code",
        "display": "Claude Code (Opus 4.7)",
        "priority": 1,
        "check_type": "process",
        "process_names": ["claude.exe", "node"],
        "process_pattern": "claude",
    },
    {
        "name": "codex",
        "display": "Codex CLI",
        "priority": 3,
        "check_type": "process",
        "process_names": ["codex.exe", "node"],
        "process_pattern": "codex",
    },
    {
        "name": "ollama",
        "display": "Ollama (gemma4:31b)",
        "priority": 4,
        "check_type": "http",
        "url": f"{OLLAMA_URL}/api/tags",
    },
]

# Qdrant/LightRAG（フォールバック先が使う共有インフラ）
INFRA_SERVICES = [
    {"name": "qdrant", "url": f"{QDRANT_URL}/collections", "required": True},
    {"name": "health_server", "url": f"{HEALTH_URL}/health", "required": False},
    {"name": "ollama_embed", "url": f"{OLLAMA_URL}/api/tags", "required": False},
]


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "active_manager": None,
        "last_check": None,
        "failover_history": [],
        "services": {},
    }


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


# ---------------------------------------------------------------------------
# ヘルスチェック
# ---------------------------------------------------------------------------

def check_process(service: dict) -> bool:
    """プロセス生存確認."""
    pattern = service.get("process_pattern", "")
    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # tasklist
        for name in service.get("process_names", []):
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=no_window,
            )
            if name.lower() in result.stdout.lower():
                if not pattern or pattern in result.stdout.lower():
                    return True
                # node.exeの場合はCommandLineでフィルタ
                if name == "node":
                    ps_result = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"Get-Process -Name node -ErrorAction SilentlyContinue | "
                         f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | "
                         f"Select-Object -First 1 -ExpandProperty Id"],
                        capture_output=True, text=True, timeout=10,
                        creationflags=no_window,
                    )
                    if ps_result.stdout.strip():
                        return True
    except Exception:
        pass
    return False


def check_http(url: str) -> bool:
    """HTTP endpoint健全性確認."""
    try:
        req = urllib.request.Request(url)
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def check_service(service: dict) -> bool:
    """サービスの健全性チェック."""
    if service["check_type"] == "process":
        return check_process(service)
    elif service["check_type"] == "http":
        return check_http(service.get("url", ""))
    return False


def check_infra() -> dict:
    """共有インフラの健全性チェック."""
    results = {}
    for svc in INFRA_SERVICES:
        results[svc["name"]] = check_http(svc["url"])
    return results


# ---------------------------------------------------------------------------
# フォールバック起動
# ---------------------------------------------------------------------------

def build_failover_context() -> str:
    """フォールバック先に渡すコンテキストを構築."""
    session_info = ""
    checkpoint_info = ""

    # 最新セッションファイル
    session_files = sorted(MEMORY_DIR.glob("project_session_*.md"), reverse=True)
    if session_files:
        session_info = f"最新セッション: {session_files[0].name}"

    # チェックポイント
    if CHECKPOINT_FILE.exists():
        try:
            cp = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
            checkpoint_info = f"チェックポイント: {cp.get('timestamp', 'unknown')} (tool #{cp.get('tool_count', '?')})"
        except (json.JSONDecodeError, OSError):
            pass

    return f"""{session_info}
{checkpoint_info}
作業ディレクトリ: C:\\Development
記憶: memory/ フォルダ + Qdrant localhost:6333 + LightRAG localhost:9621
最初にMEMORY.mdを読んでください。"""


def start_codex_fallback() -> bool:
    """Codex CLIをフォールバックとして起動."""
    if not _cmd_exists("codex"):
        return False

    context = build_failover_context()
    prompt = (
        "あなたはClaude Codeの一時的な代理です。Claudeサーバーが復旧するまでユーザーをサポートしてください。\n"
        f"{context}\n"
        "制限: 大きなアーキテクチャ判断はClaude復旧まで保留。作業はmemory/failover_log.mdに記録すること。"
    )

    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen(
            ["codex", "--full-auto", "-q", prompt],
            cwd="C:\\Development",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=no_window,
        )
        _write_failover_log("codex", "Codexフォールバック起動")
        return True
    except Exception as e:
        print(f"  Codex起動失敗: {e}")
        return False


def start_ollama_fallback() -> bool:
    """Ollamaをフォールバックとして起動（assistant_daemonのキューに投入）."""
    if not check_http(f"{OLLAMA_URL}/api/tags"):
        return False

    context = build_failover_context()
    task = {
        "id": f"failover_{int(time.time())}",
        "task": "failover_monitor",
        "context": context,
        "provider": "ollama",
        "model": "gemma4:31b",
        "target": "opus",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "instructions": (
            "Claudeが停止中です。Discordメッセージを監視し、簡易対応してください。"
            "重要な判断はClaude復旧まで保留。作業はmemory/failover_log.mdに記録。"
        ),
    }

    # assistant_daemonのキューに投入
    queue_dir = Path.home() / ".helix-agent" / "assistant" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    task_file = queue_dir / f"{task['id']}.json"
    task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_failover_log("ollama", "Ollama(gemma4)フォールバック起動 → assistant_daemonキュー投入")
    return True


def _cmd_exists(cmd: str) -> bool:
    """コマンドが存在するか確認."""
    try:
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            ["where", cmd] if sys.platform == "win32" else ["which", cmd],
            capture_output=True, text=True, timeout=5,
            creationflags=no_window,
        )
        return result.returncode == 0
    except Exception:
        return False


def _write_failover_log(manager: str, action: str) -> None:
    """フォールバックログに記録."""
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = f"\n## {timestamp}\n- **管理者**: {manager}\n- **アクション**: {action}\n"

    if FAILOVER_LOG.exists():
        content = FAILOVER_LOG.read_text(encoding="utf-8")
    else:
        content = "---\nname: フォールバックログ\ndescription: AIサービスフォールバック時の作業記録\ntype: project\n---\n\n# Failover Log\n"

    content += entry
    FAILOVER_LOG.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def run_check() -> dict:
    """全サービスチェック + フォールバック判定."""
    state = load_state()
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    print("=== Failover Orchestrator ===")
    print()

    # 1. AIサービスチェック
    print("[AIサービス]")
    service_status = {}
    active_manager = None

    for svc in SERVICES:
        alive = check_service(svc)
        service_status[svc["name"]] = alive
        status_icon = "✅" if alive else "❌"
        print(f"  {status_icon} {svc['display']}")

        if alive and active_manager is None:
            active_manager = svc["name"]

    # 2. インフラチェック
    print("\n[共有インフラ]")
    infra = check_infra()
    for name, alive in infra.items():
        print(f"  {'✅' if alive else '❌'} {name}")

    # 3. フォールバック判定
    prev_manager = state.get("active_manager")
    need_failover = False

    if not service_status.get("claude_code", False):
        # Claude Codeがダウン
        if prev_manager == "claude_code" or prev_manager is None:
            need_failover = True

    if need_failover:
        # 安全策: 2回目のチェックで本当に停止しているか再確認（一時的な検出ミス防止）
        import time as _time
        _time.sleep(3)
        recheck = check_service(SERVICES[0])  # claude_code を再チェック
        if recheck:
            print("\n再チェックでClaude Code検出 → フォールバック不要（一時的な検出ミス）")
            need_failover = False
            active_manager = "claude_code"

    if need_failover:
        print("\n⚠️ Claude Code停止検出（2回確認済み）→ フォールバック起動")

        # Codex → Ollama の順で試行
        started = False
        if service_status.get("codex"):
            print("  → Codex稼働中、フォールバック不要（既に稼働）")
            active_manager = "codex"
            started = True
        elif _cmd_exists("codex"):
            print("  → Codexフォールバック起動中...")
            if start_codex_fallback():
                active_manager = "codex"
                started = True
                print("  ✅ Codex起動成功")

        if not started:
            if service_status.get("ollama"):
                print("  → Ollamaフォールバック起動中...")
                if start_ollama_fallback():
                    active_manager = "ollama"
                    started = True
                    print("  ✅ Ollama(gemma4)起動成功")

        if not started:
            print("  ❌ 全フォールバック失敗")
            active_manager = None

        # 通知
        if started:
            send_notification(
                f"🔄 **Failover**: Claude Code停止 → {active_manager}に切り替え\n"
                f"サービス状態: {service_status}"
            )
        else:
            send_notification(
                "🔴 **Failover**: 全AIサービスダウン！手動対応が必要です。\n"
                f"サービス状態: {service_status}\nインフラ: {infra}"
            )

    elif prev_manager and prev_manager != "claude_code" and service_status.get("claude_code"):
        # Claude Code復帰検出
        print(f"\n✅ Claude Code復帰検出（前回管理者: {prev_manager}）")
        active_manager = "claude_code"
        _write_failover_log("claude_code", f"Claude Code復帰。{prev_manager}から引き継ぎ。")
        send_notification(
            f"✅ **Failover Recovery**: Claude Code復帰。{prev_manager}から引き継ぎ完了。"
        )

    # 状態更新
    state["active_manager"] = active_manager
    state["last_check"] = now_str
    state["services"] = service_status
    state["infra"] = infra

    if need_failover:
        state.setdefault("failover_history", []).append({
            "time": now_str,
            "from": prev_manager,
            "to": active_manager,
            "services": service_status,
        })
        state["failover_history"] = state["failover_history"][-50:]

    save_state(state)

    print(f"\n現在の管理者: {active_manager or '不明'}")
    return state


def show_status():
    """現在の状態を表示."""
    state = load_state()
    print("=== Failover Status ===")
    print(f"  管理者: {state.get('active_manager', 'unknown')}")
    print(f"  最終チェック: {state.get('last_check', 'なし')}")

    services = state.get("services", {})
    if services:
        print(f"  サービス: {services}")

    infra = state.get("infra", {})
    if infra:
        print(f"  インフラ: {infra}")

    history = state.get("failover_history", [])
    if history:
        print(f"\n  フォールバック履歴 ({len(history)}件):")
        for h in history[-5:]:
            print(f"    [{h['time'][:19]}] {h.get('from', '?')} → {h.get('to', '?')}")


def recover():
    """Claude復帰時の引き継ぎ処理."""
    state = load_state()
    prev = state.get("active_manager")

    if prev and prev != "claude_code":
        print(f"前回管理者: {prev}")
        print(f"フォールバックログ: {FAILOVER_LOG}")
        if FAILOVER_LOG.exists():
            print("\n--- Failover Log ---")
            print(FAILOVER_LOG.read_text(encoding="utf-8")[-2000:])
    else:
        print("フォールバック履歴なし。通常運用中。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif len(sys.argv) > 1 and sys.argv[1] == "recover":
        recover()
    else:
        run_check()
