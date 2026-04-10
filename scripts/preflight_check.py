"""Preflight Check — Claude Code起動前の互換性検証.

Claude Codeの自動更新で壊れうるポイントを起動前にチェックし、
問題があればロールバックまたは通知する。

トークン消費: ゼロ（ローカルチェックのみ）

チェック項目:
  1. claude CLI が応答するか（--version）
  2. Node.js バージョン互換性
  3. settings.json 構造の妥当性
  4. Hook スクリプトの構文エラー
  5. MCP サーバーの起動可能性
  6. 前回のバージョンとの差分検出

使い方:
    python scripts/preflight_check.py          # 全チェック
    python scripts/preflight_check.py --fix    # 問題を自動修復（可能な範囲）
    python scripts/preflight_check.py status   # バージョン情報表示
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

STATE_DIR = Path.home() / ".helix-agent" / "preflight"
STATE_FILE = STATE_DIR / "state.json"
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"
HOOKS_DIR = Path.home() / ".claude" / "hooks"

# Discord通知
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# 既知のClaude CLIコマンド名（フォールバック順）
CLI_CANDIDATES = ["claude", "claude-code", "npx @anthropic-ai/claude-code"]

# ---------------------------------------------------------------------------
# 状態管理
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_check": None, "cli_version": None, "cli_command": "claude", "node_version": None, "history": []}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
# チェック関数
# ---------------------------------------------------------------------------

NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def check_cli() -> dict:
    """Claude CLIの応答チェック + コマンド名の自動検出."""
    for cmd in CLI_CANDIDATES:
        try:
            parts = cmd.split()
            result = subprocess.run(
                parts + ["--version"],
                capture_output=True, text=True, timeout=15,
                creationflags=NO_WINDOW,
            )
            if result.returncode == 0 and result.stdout.strip():
                version = result.stdout.strip()
                return {"ok": True, "command": cmd, "version": version}
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue

    return {"ok": False, "command": None, "version": None, "error": "Claude CLI が見つかりません"}


def check_node() -> dict:
    """Node.jsバージョンチェック."""
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=NO_WINDOW,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            # v22.x.x 以上を推奨
            match = re.match(r'v(\d+)', version)
            major = int(match.group(1)) if match else 0
            return {
                "ok": major >= 20,
                "version": version,
                "major": major,
                "warning": f"Node.js {version} は古い可能性があります" if major < 20 else None,
            }
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return {"ok": False, "version": None, "error": "Node.js が見つかりません"}


def check_settings() -> dict:
    """settings.json の構造検証."""
    issues = []

    if not SETTINGS_FILE.exists():
        return {"ok": False, "issues": ["settings.json が存在しません"]}

    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"ok": False, "issues": [f"JSON パースエラー: {e}"]}

    # 必須キーの確認
    required_keys = ["model", "permissions", "hooks"]
    for key in required_keys:
        if key not in settings:
            issues.append(f"必須キー '{key}' が見つかりません")

    # model 形式チェック
    model = settings.get("model", "")
    if model and not re.match(r'claude-[\w-]+\[\d+[mk]?\]$', model):
        issues.append(f"model 形式が不正: '{model}' (期待: claude-*[context])")

    # hooks 構造チェック
    hooks = settings.get("hooks", {})
    if "PreToolUse" in hooks:
        for hook_group in hooks["PreToolUse"]:
            for hook in hook_group.get("hooks", []):
                cmd = hook.get("command", "")
                # フックスクリプトの存在確認
                parts = cmd.split()
                if len(parts) >= 2:
                    script_path = Path(parts[-1])
                    if not script_path.exists():
                        issues.append(f"フックスクリプト不在: {script_path}")

    # permissions 構造チェック
    perms = settings.get("permissions", {})
    if "allow" not in perms and "deny" not in perms:
        issues.append("permissions に allow/deny がありません")

    # mcpServers チェック
    mcp = settings.get("mcpServers", {})
    for name, config in mcp.items():
        if config.get("enabled") is False:
            continue
        cmd = config.get("command", "")
        if cmd and cmd not in ("node", "npx", "python", "uv", "codex"):
            # カスタムコマンドの存在確認
            cmd_path = Path(cmd)
            if not cmd_path.exists():
                try:
                    result = subprocess.run(
                        ["where", cmd] if sys.platform == "win32" else ["which", cmd],
                        capture_output=True, text=True, timeout=5,
                        creationflags=NO_WINDOW,
                    )
                    if result.returncode != 0:
                        issues.append(f"MCP '{name}': コマンド '{cmd}' が見つかりません")
                except Exception:
                    pass

    return {"ok": len(issues) == 0, "issues": issues}


def check_hooks() -> dict:
    """フックスクリプトの構文チェック."""
    issues = []
    hook_files = [
        HOOKS_DIR / "pretool_security.py",
        HOOKS_DIR / "smart_approval.py",
        HOOKS_DIR / "session_checkpoint.py",
        HOOKS_DIR / "failure_learner.py",
    ]

    for hook_file in hook_files:
        if not hook_file.exists():
            issues.append(f"不在: {hook_file.name}")
            continue
        try:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(hook_file)],
                capture_output=True, text=True, timeout=10,
                creationflags=NO_WINDOW,
            )
            if result.returncode != 0:
                issues.append(f"構文エラー: {hook_file.name}: {result.stderr[:100]}")
        except Exception as e:
            issues.append(f"検証失敗: {hook_file.name}: {e}")

    return {"ok": len(issues) == 0, "issues": issues}


def check_version_change(state: dict, current_cli: dict) -> dict:
    """バージョン変更の検出."""
    prev_version = state.get("cli_version")
    curr_version = current_cli.get("version")

    if not prev_version or not curr_version:
        return {"changed": False}

    if prev_version != curr_version:
        return {
            "changed": True,
            "previous": prev_version,
            "current": curr_version,
            "message": f"Claude Code更新検出: {prev_version} → {curr_version}",
        }

    return {"changed": False}


# ---------------------------------------------------------------------------
# 自動修復
# ---------------------------------------------------------------------------


def attempt_fix(check_results: dict) -> list[str]:
    """可能な範囲で自動修復を試みる."""
    fixes = []

    # CLI コマンド名が変わった場合 → start_claude.bat を更新
    cli = check_results.get("cli", {})
    if cli.get("ok") and cli.get("command") != "claude":
        new_cmd = cli["command"]
        bat_path = Path("C:/Development/start/manual/start_claude.bat")
        if bat_path.exists():
            content = bat_path.read_text(encoding="utf-8")
            # claude コマンドの呼び出しを更新
            if "claude --model" in content or "claude -p" in content:
                updated = content.replace("claude --model", f"{new_cmd} --model")
                updated = updated.replace("claude -p", f"{new_cmd} -p")
                bat_path.write_text(updated, encoding="utf-8")
                fixes.append(f"start_claude.bat のCLIコマンドを '{new_cmd}' に更新")

    # settings.json のmodel形式が変わった場合の修復は危険なので通知のみ
    settings = check_results.get("settings", {})
    if not settings.get("ok"):
        for issue in settings.get("issues", []):
            if "model 形式" in issue:
                fixes.append(f"[手動対応必要] {issue}")

    return fixes


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def run_all_checks(auto_fix: bool = False) -> dict:
    """全チェック実行."""
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    results = {}
    all_ok = True

    print("=== Preflight Check ===")
    print()

    # 1. CLI
    print("[1/5] Claude CLI...")
    cli = check_cli()
    results["cli"] = cli
    if cli["ok"]:
        print(f"  OK: {cli['command']} ({cli['version']})")
    else:
        print(f"  NG: {cli.get('error', 'unknown')}")
        all_ok = False

    # 2. Node.js
    print("[2/5] Node.js...")
    node = check_node()
    results["node"] = node
    if node["ok"]:
        print(f"  OK: {node['version']}")
    else:
        msg = node.get("warning") or node.get("error", "unknown")
        print(f"  WARN: {msg}")
        if node.get("error"):
            all_ok = False

    # 3. settings.json
    print("[3/5] settings.json...")
    settings = check_settings()
    results["settings"] = settings
    if settings["ok"]:
        print("  OK: 構造正常")
    else:
        for issue in settings["issues"]:
            print(f"  NG: {issue}")
        all_ok = False

    # 4. Hooks
    print("[4/5] Hook scripts...")
    hooks = check_hooks()
    results["hooks"] = hooks
    if hooks["ok"]:
        print("  OK: 全フック正常")
    else:
        for issue in hooks["issues"]:
            print(f"  NG: {issue}")
        all_ok = False

    # 5. バージョン変更検出
    print("[5/5] Version change...")
    version_change = check_version_change(state, cli)
    results["version_change"] = version_change
    if version_change.get("changed"):
        msg = version_change["message"]
        print(f"  UPDATE: {msg}")
        # Discord通知
        send_notification(f"Preflight: {msg}")
    else:
        print("  OK: バージョン変更なし")

    # 自動修復
    if auto_fix and not all_ok:
        print("\n[自動修復]")
        fixes = attempt_fix(results)
        results["fixes"] = fixes
        for fix in fixes:
            print(f"  {fix}")
        if not fixes:
            print("  自動修復可能な項目なし")

    # 状態保存
    state["last_check"] = now
    if cli.get("ok"):
        prev_version = state.get("cli_version")
        state["cli_version"] = cli["version"]
        state["cli_command"] = cli["command"]

        # バージョン変更履歴
        if prev_version and prev_version != cli["version"]:
            state.setdefault("history", []).append({
                "time": now,
                "from": prev_version,
                "to": cli["version"],
            })
            state["history"] = state["history"][-20:]

    if node.get("ok"):
        state["node_version"] = node["version"]

    state["last_result"] = "OK" if all_ok else "NG"
    save_state(state)

    # 全体結果
    print(f"\n{'OK - 全チェック正常' if all_ok else 'NG - 問題あり（上記確認）'}")

    # NG時はDiscord通知
    if not all_ok:
        issues = []
        for key, val in results.items():
            if isinstance(val, dict) and not val.get("ok", True):
                for issue in val.get("issues", [val.get("error", "unknown")]):
                    issues.append(f"{key}: {issue}")
        if issues:
            send_notification(
                f"Preflight NG:\n" + "\n".join(f"- {i}" for i in issues[:5])
            )

    return results


def show_status():
    """バージョン情報と履歴を表示."""
    state = load_state()
    print("=== Preflight Status ===")
    print(f"  最終チェック: {state.get('last_check', 'なし')}")
    print(f"  最終結果: {state.get('last_result', 'なし')}")
    print(f"  CLI: {state.get('cli_command', '?')} {state.get('cli_version', '?')}")
    print(f"  Node: {state.get('node_version', '?')}")

    history = state.get("history", [])
    if history:
        print(f"\n  バージョン更新履歴 ({len(history)}件):")
        for h in history[-5:]:
            print(f"    [{h['time'][:19]}] {h.get('from', '?')} → {h.get('to', '?')}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif "--fix" in sys.argv:
        run_all_checks(auto_fix=True)
    else:
        run_all_checks()
