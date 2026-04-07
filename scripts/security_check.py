"""セキュリティ・バージョンチェッカー.

依存ツールの現在バージョンを一覧表示し、
GitHub Releases APIで最新バージョンと比較。
更新があればDiscord通知。

使い方:
    python scripts/security_check.py              # バージョン一覧
    python scripts/security_check.py --update     # 更新チェック+Discord通知
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

JST = timezone(timedelta(hours=9))
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"
CACHE_FILE = Path.home() / ".helix-agent" / "security" / "versions.json"

# 監視対象ツール
TOOLS = [
    {
        "name": "Claude Code",
        "cmd": "claude --version",
        "parse": r"(\d+\.\d+\.\d+)",
        "github": None,  # npmで管理
        "npm": "@anthropic-ai/claude-code",
    },
    {
        "name": "Ollama",
        "cmd": "ollama --version",
        "parse": r"(\d+\.\d+\.\d+)",
        "github": "ollama/ollama",
    },
    {
        "name": "agent-browser",
        "cmd": "agent-browser --version",
        "parse": r"(\d+\.\d+\.\d+)",
        "github": "anthropics/agent-browser",
    },
    {
        "name": "Python",
        "cmd": "python --version",
        "parse": r"(\d+\.\d+\.\d+)",
        "github": None,
    },
    {
        "name": "Node.js",
        "cmd": "node --version",
        "parse": r"v?(\d+\.\d+\.\d+)",
        "github": None,
    },
    {
        "name": "uv",
        "cmd": "uv --version",
        "parse": r"(\d+\.\d+\.\d+)",
        "github": "astral-sh/uv",
    },
    {
        "name": "fieldtheory",
        "cmd": "ft --version",
        "parse": r"(\d+\.\d+\.\d+)",
        "github": None,
    },
    {
        "name": "Codex CLI",
        "cmd": "codex --version",
        "parse": r"(\d+\.\d+\.\d+)",
        "github": None,
        "npm": "@anthropic-ai/codex",
    },
]


def get_local_version(tool: dict) -> str | None:
    """ローカルのバージョンを取得."""
    try:
        result = subprocess.run(
            tool["cmd"], shell=True, capture_output=True, text=True,
            timeout=10, encoding="utf-8", errors="replace",
        )
        output = result.stdout.strip() + result.stderr.strip()
        match = re.search(tool["parse"], output)
        return match.group(1) if match else None
    except Exception:
        return None


def get_github_latest(repo: str) -> str | None:
    """GitHub APIから最新リリースバージョンを取得."""
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        req = Request(url, headers={"Accept": "application/vnd.github.v3+json"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            tag = data.get("tag_name", "")
            match = re.search(r"(\d+\.\d+\.\d+)", tag)
            return match.group(1) if match else tag
    except (URLError, json.JSONDecodeError, KeyError):
        return None


def version_tuple(v: str) -> tuple:
    """バージョン文字列をタプルに変換."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def check_all(notify: bool = False):
    """全ツールのバージョンチェック."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(JST)
    results = []
    updates = []

    print(f"=== Security Version Check ({now.strftime('%Y-%m-%d %H:%M')}) ===")
    print()

    for tool in TOOLS:
        local = get_local_version(tool)
        latest = None
        status = "OK"

        if tool.get("github"):
            latest = get_github_latest(tool["github"])
            if local and latest:
                if version_tuple(latest) > version_tuple(local):
                    status = "UPDATE"
                    updates.append(f"{tool['name']}: {local} -> {latest}")

        local_str = local or "N/A"
        latest_str = latest or "-"
        flag = " << UPDATE" if status == "UPDATE" else ""

        print(f"  {tool['name']:20s} local={local_str:12s} latest={latest_str:12s}{flag}")

        results.append({
            "name": tool["name"],
            "local": local,
            "latest": latest,
            "status": status,
        })

    # キャッシュ保存
    cache = {
        "timestamp": now.isoformat(),
        "results": results,
        "updates_available": len(updates),
    }
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    if updates:
        print()
        print(f"  ** {len(updates)}件の更新あり **")
        for u in updates:
            print(f"    - {u}")

        if notify:
            msg = f"**Security Update Alert**\n{len(updates)}件の更新:\n"
            msg += "\n".join(f"- {u}" for u in updates)
            try:
                subprocess.run(
                    ["python", str(WEBHOOK_SCRIPT), msg],
                    timeout=15, capture_output=True,
                )
            except Exception:
                pass
    else:
        print()
        print("  All up to date.")


if __name__ == "__main__":
    notify = "--update" in sys.argv
    check_all(notify=notify)
