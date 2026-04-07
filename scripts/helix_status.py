"""Helix全体ダッシュボード — 全コンポーネントの状態を一覧表示.

supervisor.py の DAEMONS / SERVICES 辞書を唯一の定義元として読み取り、
一覧表示する。追加・修正は supervisor.py の1箇所だけでOK。

使い方:
    python scripts/helix_status.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# supervisor.py の辞書をJSON経由で読み込み（import時の副作用を回避）
def _load_supervisor_config() -> tuple[dict, dict]:
    """supervisor.pyからDAEMONS/SERVICESを安全に読み込む."""
    import ast
    sup_path = Path(__file__).resolve().parent / "supervisor.py"
    text = sup_path.read_text(encoding="utf-8")
    # DAEMONS = { ... } と SERVICES = { ... } を抽出
    daemons = {}
    services = {}
    for line_no, line in enumerate(text.split("\n")):
        if line.startswith("DAEMONS = {"):
            # 辞書の終わりまで抽出
            start = text.index("DAEMONS = {")
            depth = 0
            for i, ch in enumerate(text[start:], start):
                if ch == "{": depth += 1
                elif ch == "}": depth -= 1
                if depth == 0:
                    block = text[start + len("DAEMONS = "):i+1]
                    break
            # Path()をstr化してeval可能にする
            block = block.replace("SCRIPTS_DIR / ", "").replace('Path(__file__).resolve().parent / ', '')
            try:
                # 簡易パース: description, interval_min, stale_threshold_min, criticalのみ抽出
                pass
            except Exception:
                pass
        if line.startswith("SERVICES = {"):
            pass
    # フォールバック: ハードコードされた最小限の定義
    return daemons, services

# 安全な方法: supervisor.pyの辞書定義だけをJSONファイル経由で共有
# supervisor.pyが実行されるたびに設定をJSONに書き出す仕組みを使う
_CONFIG_FILE = Path.home() / ".helix-agent" / "supervisor_config.json"

def load_config():
    """supervisorが書き出した設定JSONを読む."""
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return data.get("daemons", {}), data.get("services", {})
        except Exception:
            pass
    # フォールバック
    return {}, {}

DAEMONS_CFG, SERVICES_CFG = load_config()

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

JST = timezone(timedelta(hours=9))
HELIX_DIR = Path.home() / ".helix-agent"
HB_DIR = HELIX_DIR / "heartbeats"

# ---------------------------------------------------------------------------
# チェック関数
# ---------------------------------------------------------------------------

def check_http(name: str, url: str, timeout: int = 3) -> tuple[str, str]:
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return "OK", ""
    except Exception as e:
        return "NG", str(e)[:40]


def check_heartbeat(name: str, max_age_min: int) -> tuple[str, str]:
    hb_file = HB_DIR / f"{name}.json"
    if not hb_file.exists():
        return "NG", "no heartbeat"
    try:
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
        if age > max_age_min:
            return "WARN", f"{int(age)}min ago (>{max_age_min})"
        return "OK", f"{int(age)}min ago"
    except Exception:
        return "NG", "parse error"


def check_process(name: str) -> bool:
    try:
        result = subprocess.run(
            f'tasklist /FI "IMAGENAME eq {name}" /NH',
            shell=True, capture_output=True, text=True, timeout=5,
        )
        return name.lower() in result.stdout.lower()
    except Exception:
        return False


def fmt(status: str, width: int = 4) -> str:
    """ステータス文字列のフォーマット."""
    if status == "OK":
        return "[OK]  "
    elif status == "WARN":
        return "[WARN]"
    else:
        return "[NG]  "


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(JST)
    print(f"{'='*60}")
    print(f"  HELIX DASHBOARD — {now.strftime('%Y-%m-%d %H:%M:%S')} JST")
    print(f"{'='*60}")

    # --- 1. 常駐サービス (supervisor.py SERVICES から自動読み取り) ---
    # 追加の外部サービス（supervisor管理外だが表示したいもの）
    EXTRA_SERVICES = {
        "qdrant": {"health_url": "http://localhost:6333/healthz", "description": "Qdrant Vector DB"},
        "ollama": {"health_url": "http://localhost:11434/api/version", "description": "Ollama LLM"},
        "qdrant_memory": {"health_url": "http://localhost:8080/health", "description": "Qdrant Memory API"},
        "ai_studio": {"health_url": "http://localhost:8504/", "description": "Helix AI Studio"},
    }
    all_services = {**EXTRA_SERVICES, **SERVICES_CFG}

    print(f"\n--- Services (HTTP Health) [{len(all_services)}] ---")
    for name, svc in all_services.items():
        url = svc.get("health_url", "")
        desc = svc.get("description", name)
        if url:
            status, detail = check_http(name, url)
            print(f"  {fmt(status)} {desc:28s} {detail}")

    # --- 2. デーモン (supervisor_config.json から自動読み取り + supervisor自身) ---
    print(f"\n--- Daemons (Heartbeat) [{len(DAEMONS_CFG) + 1}] ---")
    # supervisor自身
    status, detail = check_heartbeat("supervisor", 10)
    print(f"  {fmt(status)} {'supervisor':28s} {detail}")
    # DAEMONS辞書から
    for name, config in DAEMONS_CFG.items():
        threshold = config.get("stale_threshold_min", 15)
        desc = config.get("description", name)
        status, detail = check_heartbeat(name, threshold)
        print(f"  {fmt(status)} {name} ({desc})"[:48].ljust(48) + f" {detail}")

    # --- 3. プロセス ---
    print(f"\n--- Processes ---")
    processes = [
        ("chrome.exe",  "Chrome"),
        ("ollama.exe",  "Ollama"),
        ("qdrant.exe",  "Qdrant"),
        ("claude.exe",  "Claude CLI"),
        ("node.exe",    "Node.js"),
        ("codex.exe",   "Codex CLI"),
    ]
    for proc, label in processes:
        running = check_process(proc)
        status = "OK" if running else "NG"
        print(f"  {fmt(status)} {label:18s} {'running' if running else 'not found'}")

    # --- 4. セキュリティ ---
    print(f"\n--- Security ---")
    sec_file = HELIX_DIR / "security" / "alerts.json"
    if sec_file.exists():
        data = json.loads(sec_file.read_text(encoding="utf-8"))
        crit = data.get("critical", 0)
        warn = data.get("warning", 0)
        ts = data.get("timestamp", "?")
        status = "OK" if crit == 0 else "NG"
        print(f"  {fmt(status)} Last scan: {ts[:19]}  Critical:{crit} Warning:{warn}")
    else:
        print(f"  {fmt('NG')} No scan data")

    ver_file = HELIX_DIR / "security" / "versions.json"
    if ver_file.exists():
        data = json.loads(ver_file.read_text(encoding="utf-8"))
        updates = data.get("updates_available", 0)
        status = "OK" if updates == 0 else "WARN"
        print(f"  {fmt(status)} Tool updates: {updates} available")

    hook = Path.home() / ".claude" / "hooks" / "pretool_security.py"
    print(f"  {fmt('OK' if hook.exists() else 'NG')} GlassWorm hook: {'active' if hook.exists() else 'MISSING'}")

    # --- 5. Claude使用率 ---
    print(f"\n--- Claude Usage ---")
    usage_file = HELIX_DIR / "claude_usage" / "latest.json"
    if usage_file.exists():
        data = json.loads(usage_file.read_text(encoding="utf-8"))
        weekly = data.get("weekly_all", {})
        sonnet = data.get("sonnet_only", {})
        session = data.get("session", {})
        w_pct = weekly.get("percent", "?")
        s_pct = sonnet.get("percent", "?")
        w_status = "OK" if isinstance(w_pct, int) and w_pct < 70 else ("WARN" if isinstance(w_pct, int) and w_pct < 85 else "NG")
        s_status = "OK" if isinstance(s_pct, int) and s_pct < 70 else ("WARN" if isinstance(s_pct, int) and s_pct < 85 else "NG")
        print(f"  {fmt(w_status)} Weekly (All):  {w_pct}%  reset: {weekly.get('reset', '?')}")
        print(f"  {fmt(s_status)} Sonnet only:   {s_pct}%  reset: {sonnet.get('reset', '?')}")
        print(f"  {'      '} Updated: {data.get('timestamp', '?')[:19]}")
    else:
        print(f"  {fmt('NG')} No usage data")

    # --- 6. Codex Usage ---
    print(f"\n--- Codex Usage ---")
    codex_file = HELIX_DIR / "claude_usage" / "codex_latest.json"
    if codex_file.exists():
        data = json.loads(codex_file.read_text(encoding="utf-8"))
        for key, info in data.get("limits", {}).items():
            remaining = info.get("remaining_percent", "?")
            used = info.get("used_percent", 0)
            status = "OK" if isinstance(used, int) and used < 70 else ("WARN" if isinstance(used, int) and used < 85 else "NG")
            label = info.get("label", key)[:30]
            print(f"  {fmt(status)} {label:32s} used:{used}% remaining:{remaining}%")
        print(f"  {'      '} Updated: {data.get('timestamp', '?')[:19]}")
    else:
        print(f"  {fmt('NG')} No Codex usage data")

    # --- 7. 共有記憶 ---
    print(f"\n--- Shared Memory (Qdrant) ---")
    try:
        resp = urllib.request.urlopen("http://localhost:8080/health", timeout=3)
        data = json.loads(resp.read())
        print(f"  {fmt('OK')} {data.get('backend', '?')} user={data.get('user_id', '?')}")
    except Exception:
        print(f"  {fmt('NG')} Connection failed")

    print(f"\n{'='*60}")


if __name__ == "__main__":
    main()
