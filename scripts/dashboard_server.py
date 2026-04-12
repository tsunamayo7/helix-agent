"""Helix Dashboard Server — ブラウザベースのシステム状態可視化.

トークン消費: 完全にゼロ（Pure Python HTTPサーバー、API呼び出しなし）
セキュリティ: localhost のみバインド（外部アクセス不可）、読み取り専用

機能:
  - PC部品状態（GPU温度/VRAM/電力、CPU使用率、RAM、ディスク）
  - AIサービス状態（Claude/Codex/Ollama/Qdrant）
  - デーモン心拍一覧
  - 会話ログ閲覧
  - Watchdog/Failoverアラート履歴

使い方:
    python scripts/dashboard_server.py              # サーバー起動 (port 8801)
    python scripts/dashboard_server.py --port 9000  # ポート指定
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

PORT = 8801
HELIX_DIR = Path.home() / ".helix-agent"
MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
CONV_LOG_DIR = HELIX_DIR / "conversation_logs"

HW_STATUS = HELIX_DIR / "hw_monitor" / "hw_status.json"
WATCHDOG_STATE = HELIX_DIR / "watchdog" / "state.json"
FAILOVER_STATE = HELIX_DIR / "failover" / "state.json"
HEARTBEATS_DIR = HELIX_DIR / "heartbeats"
CHECKPOINT_FILE = MEMORY_DIR / "session_checkpoint.json"

SERVICE_URLS = {
    "qdrant": "http://localhost:6333/collections",
    "ollama": "http://localhost:11434/api/tags",
    "health_server": "http://localhost:8800/health",
}


# ---------------------------------------------------------------------------
# データ収集（全てローカルファイル読み取り、API呼び出しなし）
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _check_http(url: str, timeout: int = 3) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def _age_str(ts_str: str) -> str:
    """タイムスタンプからの経過時間を文字列で返す."""
    if not ts_str:
        return "不明"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}秒前"
        elif secs < 3600:
            return f"{secs // 60}分前"
        elif secs < 86400:
            return f"{secs // 3600}時間{(secs % 3600) // 60}分前"
        else:
            return f"{secs // 86400}日前"
    except (ValueError, TypeError):
        return "不明"


def collect_hw_status() -> dict:
    data = _read_json(HW_STATUS) or {}
    gpus = data.get("gpus", [])
    cpu = data.get("cpu", {})
    ts = data.get("timestamp", "")
    return {
        "timestamp": ts,
        "age": _age_str(ts),
        "gpus": gpus,
        "cpu": cpu,
        "alerts": data.get("alerts", []),
        "disk": _get_disk_info(),
    }


def _get_disk_info() -> list[dict]:
    """ディスク使用量を取得."""
    disks = []
    try:
        import shutil
        for drive in ["C:", "D:", "E:", "F:"]:
            try:
                usage = shutil.disk_usage(drive + "\\")
                disks.append({
                    "drive": drive,
                    "total_gb": round(usage.total / (1024**3), 1),
                    "used_gb": round(usage.used / (1024**3), 1),
                    "free_gb": round(usage.free / (1024**3), 1),
                    "usage_pct": round(usage.used / usage.total * 100, 1),
                })
            except (OSError, FileNotFoundError):
                continue
    except ImportError:
        pass
    return disks


def collect_services() -> dict:
    failover = _read_json(FAILOVER_STATE) or {}
    services = {}
    for name, url in SERVICE_URLS.items():
        services[name] = _check_http(url)
    services["claude_code"] = failover.get("services", {}).get("claude_code", False)
    services["codex"] = failover.get("services", {}).get("codex", False)
    return {
        "services": services,
        "active_manager": failover.get("active_manager", "不明"),
        "last_check": failover.get("last_check", ""),
        "age": _age_str(failover.get("last_check", "")),
        "failover_history": failover.get("failover_history", [])[-5:],
    }


def collect_daemons() -> list[dict]:
    daemons = []
    if HEARTBEATS_DIR.exists():
        for hb_file in sorted(HEARTBEATS_DIR.glob("*.json")):
            data = _read_json(hb_file)
            if data:
                ts = data.get("timestamp", "")
                age = _age_str(ts)
                # 10分以上応答なし = dead
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
                    status = "alive" if age_min < 10 else "stale" if age_min < 30 else "dead"
                except (ValueError, TypeError):
                    status = "unknown"
                    age_min = -1

                daemons.append({
                    "name": data.get("daemon", hb_file.stem),
                    "pid": data.get("pid", 0),
                    "timestamp": ts,
                    "age": age,
                    "status": status,
                    "age_min": round(age_min, 1) if age_min >= 0 else -1,
                })
    return daemons


def collect_watchdog() -> dict:
    data = _read_json(WATCHDOG_STATE) or {}
    return {
        "cli_running": data.get("cli_running", False),
        "last_cli_seen": data.get("last_cli_seen", ""),
        "last_cli_age": _age_str(data.get("last_cli_seen", "")),
        "last_check": data.get("last_check", ""),
        "last_check_age": _age_str(data.get("last_check", "")),
        "alert_count": len(data.get("alert_history", [])),
        "recent_alerts": [
            {"time": a.get("time", "")[:19], "message": a.get("message", "")[:120]}
            for a in data.get("alert_history", [])[-5:]
        ],
    }


def collect_conversation_logs() -> list[dict]:
    logs = []
    if CONV_LOG_DIR.exists():
        for md_file in sorted(CONV_LOG_DIR.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
            stat = md_file.stat()
            logs.append({
                "name": md_file.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return logs


def collect_checkpoint() -> dict:
    data = _read_json(CHECKPOINT_FILE) or {}
    ts = data.get("timestamp", "")
    return {
        "timestamp": ts,
        "age": _age_str(ts),
        "tool_count": data.get("tool_count", 0),
    }


def collect_audit() -> dict:
    """最新監査レポートを読み込み."""
    audit_file = HELIX_DIR / "audit_reports" / "latest.json"
    data = _read_json(audit_file)
    if not data:
        return {"available": False}
    return {
        "available": True,
        "timestamp_jst": data.get("timestamp_jst", ""),
        "summary": data.get("summary", {}),
        "findings": data.get("findings", [])[:10],
        "improvements": data.get("improvements", [])[:5],
    }


def collect_anomaly_queue() -> dict:
    """anomaly_queue.json を読み取り."""
    path = HELIX_DIR / "anomaly_queue.json"
    data = _read_json(path)
    if not data:
        return {"pending_count": 0, "resolved_count": 0, "pending": [], "last_updated": None}
    return {
        "pending_count": len(data.get("pending", [])),
        "resolved_count": len(data.get("resolved", [])),
        "pending": data.get("pending", [])[:10],  # 上位10件
        "last_updated": data.get("last_updated"),
    }


def collect_dept_rag() -> dict:
    """部門RAGのポイント数を取得."""
    depts = ["dept_hr", "dept_research", "dept_design", "dept_build", "dept_qa", "mem0_shared"]
    result = {}
    for d in depts:
        try:
            resp = urllib.request.urlopen(f"http://localhost:6333/collections/{d}", timeout=2)
            info = json.loads(resp.read().decode())
            result[d] = info.get("result", {}).get("points_count", 0)
        except Exception:
            result[d] = None
    return result


def collect_scheduled_tasks() -> list[dict]:
    """Helix系スケジュールタスクの状態を取得 (1回のPowerShell呼び出しで一括)."""
    import subprocess
    tasks_to_check = [
        "Helix-Supervisor", "Helix-AssistantDaemon", "Helix-Watchdog",
        "helix-hw-monitor", "Helix-UsageMonitor", "Helix-HealthServer",
        "helix-x-monitor", "X-Feed-Collector", "Helix-CMEMBridge",
        "Helix-ConversationLogger", "Helix-Backup", "Helix-IntegrityCheck",
        "Helix-MemoryHealth", "Helix-ContradictionCheck", "Helix-QdrantDedup",
        "Helix-Failover", "Helix-Escalation", "Helix-DeptFeed",
        "Helix-SystemAudit", "Helix-CriticalGuard", "Helix-TimeoutChecker",
    ]
    # 全タスクを1回のPowerShell呼び出しで一括取得 (21回→1回に削減)
    names_array = ",".join(f"'{n}'" for n in tasks_to_check)
    ps_cmd = (
        f"foreach($n in @({names_array})) {{"
        f"  $i = Get-ScheduledTaskInfo -TaskName $n -EA SilentlyContinue; "
        f"  if (-not $i) {{ Write-Output \"$n::MISSING\" }} "
        f"  else {{ Write-Output \"$n::$($i.LastRunTime.ToString('o'))|$($i.LastTaskResult)\" }}"
        f"}}"
    )
    tasks = []
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        output_lines = result.stdout.strip().splitlines()
        parsed_names = set()
        for line in output_lines:
            line = line.strip()
            if "::" not in line:
                continue
            name, value = line.split("::", 1)
            parsed_names.add(name)
            if value == "MISSING":
                tasks.append({"name": name, "status": "missing", "last_run": None, "result": None})
                continue
            parts = value.split("|")
            if len(parts) == 2:
                last_run, result_code = parts
                if last_run.startswith("1999"):
                    tasks.append({"name": name, "status": "never_run", "last_run": None, "result": None})
                else:
                    try:
                        lr = datetime.fromisoformat(last_run)
                        if lr.tzinfo is None:
                            lr = lr.replace(tzinfo=timezone.utc)
                        age_min = (datetime.now(timezone.utc) - lr).total_seconds() / 60
                        tasks.append({
                            "name": name,
                            "status": "ok" if result_code.strip() in ("0", "267011") else "failed",
                            "last_run": last_run[:19],
                            "age_min": int(age_min),
                            "result": result_code.strip(),
                        })
                    except Exception:
                        tasks.append({"name": name, "status": "parse_error", "last_run": last_run, "result": result_code})
        # PowerShell出力に含まれなかったタスクをcheck_failedとして追加
        for name in tasks_to_check:
            if name not in parsed_names:
                tasks.append({"name": name, "status": "check_failed", "last_run": None, "result": None})
    except Exception:
        # PowerShell自体が失敗した場合、全タスクをcheck_failedに
        for name in tasks_to_check:
            tasks.append({"name": name, "status": "check_failed", "last_run": None, "result": None})
    return tasks


def collect_auto_mode() -> dict:
    """自律承認モードの状態."""
    path = HELIX_DIR / "auto_mode" / "state.json"
    data = _read_json(path)
    if not data:
        return {"active": False, "pending_since": None, "unlock_until": None, "approved": 0, "denied": 0}
    now = datetime.now(timezone.utc)
    unlock = data.get("unlock_until")
    active = False
    if unlock:
        try:
            unlock_dt = datetime.fromisoformat(unlock)
            active = now < unlock_dt
        except Exception:
            pass
    return {
        "active": active,
        "pending_since": data.get("pending_since"),
        "unlock_until": unlock,
        "approved": data.get("auto_approved_count", 0),
        "denied": data.get("auto_denied_count", 0),
        "last_user_reply": data.get("last_user_reply"),
    }


def collect_critical_guard() -> dict:
    """Critical Files Guardの状態."""
    state = _read_json(HELIX_DIR / "critical_guard" / "state.json")
    if not state:
        return {"tracked_files": 0, "last_run": None, "recent_changes": []}

    # 変更ログ末尾10件
    change_log = HELIX_DIR / "critical_guard" / "change_log.jsonl"
    recent = []
    if change_log.exists():
        try:
            lines = change_log.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-10:]:
                try:
                    entry = json.loads(line)
                    recent.append({
                        "event": entry.get("event", ""),
                        "path": Path(entry.get("path", "")).name,
                        "timestamp": entry.get("timestamp", "")[:19],
                    })
                except Exception:
                    pass
        except Exception:
            pass

    return {
        "tracked_files": len(state.get("files", {})),
        "last_run": state.get("last_run"),
        "recent_changes": recent[::-1],  # 新しい順
    }


def collect_audit_report() -> dict:
    """latest audit reportのサマリー."""
    path = HELIX_DIR / "audit_reports" / "latest.json"
    data = _read_json(path)
    if not data:
        return {"available": False}
    findings = data.get("findings", [])
    by_severity = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        sev = f.get("severity", "").upper()
        if sev in by_severity:
            by_severity[sev] += 1
    return {
        "available": True,
        "timestamp": data.get("timestamp"),
        "total_findings": len(findings),
        "by_severity": by_severity,
    }


# ---------------------------------------------------------------------------
# POST アクション (ブラウザからの手動実行)
# ---------------------------------------------------------------------------


def run_self_heal_action() -> dict:
    """env_self_heal.py を実行して結果を返す."""
    import subprocess
    try:
        result = subprocess.run(
            [
                "C:\\Program Files\\Python312\\python.exe",
                "C:\\Development\\tools\\helix-agent\\scripts\\env_self_heal.py",
            ],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return {
            "success": result.returncode == 0,
            "output": (result.stdout or "").strip()[:2000],
            "error": (result.stderr or "").strip()[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_audit_action() -> dict:
    """system_auditor.py --quick を実行."""
    import subprocess
    try:
        result = subprocess.run(
            [
                "C:\\Program Files\\Python312\\python.exe",
                "C:\\Development\\tools\\helix-agent\\scripts\\system_auditor.py",
                "--quick",
            ],
            capture_output=True, text=True, timeout=120,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        # anomaly_dispatcher も連鎖実行
        subprocess.run(
            [
                "C:\\Program Files\\Python312\\python.exe",
                "C:\\Development\\tools\\helix-agent\\scripts\\anomaly_dispatcher.py",
            ],
            capture_output=True, timeout=30,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return {
            "success": result.returncode == 0,
            "output": (result.stdout or "").strip()[:3000],
            "error": (result.stderr or "").strip()[:500],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def clear_anomaly_queue() -> dict:
    """anomaly_dispatcher.py clear を実行."""
    import subprocess
    try:
        result = subprocess.run(
            [
                "C:\\Program Files\\Python312\\python.exe",
                "C:\\Development\\tools\\helix-agent\\scripts\\anomaly_dispatcher.py",
                "clear",
            ],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return {
            "success": result.returncode == 0,
            "output": (result.stdout or "").strip(),
            "error": (result.stderr or "").strip(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def collect_all() -> dict:
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "collected_at_jst": (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S JST"),
        "hw": collect_hw_status(),
        "services": collect_services(),
        "daemons": collect_daemons(),
        "watchdog": collect_watchdog(),
        "checkpoint": collect_checkpoint(),
        "conversation_logs": collect_conversation_logs()[:20],
        "audit": collect_audit(),
        # 拡張: 管理者俯瞰ビュー
        "anomaly_queue": collect_anomaly_queue(),
        "dept_rag": collect_dept_rag(),
        "scheduled_tasks": collect_scheduled_tasks(),
        "auto_mode": collect_auto_mode(),
        "critical_guard": collect_critical_guard(),
        "audit_report": collect_audit_report(),
    }


def read_conversation_log(filename: str) -> str:
    """会話ログファイルの内容を返す."""
    safe_name = Path(filename).name  # パストラバーサル防止
    filepath = CONV_LOG_DIR / safe_name
    if filepath.exists() and filepath.suffix == ".md":
        try:
            return filepath.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


# ---------------------------------------------------------------------------
# HTML ダッシュボード
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Helix Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --purple: #bc8cff;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text);
    padding: 16px; line-height: 1.5;
  }
  h1 { font-size: 1.4em; margin-bottom: 8px; color: var(--accent); }
  h2 { font-size: 1.1em; margin-bottom: 8px; color: var(--purple); }
  .header { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:8px; }
  .header-info { color: var(--dim); font-size: 0.85em; }
  .refresh-btn {
    background: var(--accent); color: #fff; border:none; padding:8px 20px;
    border-radius:6px; cursor:pointer; font-size:0.9em; font-weight:600;
  }
  .refresh-btn:hover { opacity:0.85; }
  .refresh-btn:disabled { opacity:0.5; cursor:wait; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap:12px; margin-bottom:16px; }
  .card {
    background: var(--card); border:1px solid var(--border);
    border-radius:8px; padding:14px;
  }
  table { width:100%; border-collapse:collapse; font-size:0.85em; }
  th { text-align:left; color:var(--dim); padding:4px 8px; border-bottom:1px solid var(--border); font-weight:500; }
  td { padding:4px 8px; border-bottom:1px solid var(--border); }
  .badge {
    display:inline-block; padding:2px 8px; border-radius:12px;
    font-size:0.75em; font-weight:600;
  }
  .badge-ok { background:#0f5323; color:var(--green); }
  .badge-warn { background:#4b2d00; color:var(--yellow); }
  .badge-err { background:#5a1520; color:var(--red); }
  .badge-off { background:#2a2a2a; color:var(--dim); }
  .bar-bg { background:#21262d; border-radius:4px; height:16px; overflow:hidden; position:relative; }
  .bar-fill { height:100%; border-radius:4px; transition:width 0.3s; }
  .bar-label { position:absolute; right:6px; top:0; font-size:0.7em; line-height:16px; color:#fff; }
  .bar-green { background: var(--green); }
  .bar-yellow { background: var(--yellow); }
  .bar-red { background: var(--red); }
  .log-list { max-height:300px; overflow-y:auto; }
  .log-item {
    padding:6px 10px; cursor:pointer; border-bottom:1px solid var(--border);
    display:flex; justify-content:space-between; font-size:0.85em;
  }
  .log-item:hover { background:#1c2128; }
  .modal-overlay {
    display:none; position:fixed; top:0; left:0; right:0; bottom:0;
    background:rgba(0,0,0,0.7); z-index:100; justify-content:center; align-items:center;
  }
  .modal-overlay.active { display:flex; }
  .modal {
    background:var(--card); border:1px solid var(--border); border-radius:8px;
    width:90%; max-width:900px; max-height:85vh; overflow:hidden; display:flex; flex-direction:column;
  }
  .modal-header {
    display:flex; justify-content:space-between; align-items:center;
    padding:12px 16px; border-bottom:1px solid var(--border);
  }
  .modal-body { padding:16px; overflow-y:auto; flex:1; white-space:pre-wrap; font-size:0.82em; font-family:monospace; }
  .modal-close { background:none; border:none; color:var(--dim); font-size:1.5em; cursor:pointer; }
  .alert-item { padding:4px 0; font-size:0.82em; color:var(--yellow); }
  .auto-label { font-size:0.75em; color:var(--dim); margin-left:8px; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Helix System Dashboard</h1>
    <span class="header-info" id="updateTime">読み込み中...</span>
  </div>
  <div>
    <label><input type="checkbox" id="autoRefresh"> 自動更新(30秒)</label>
    <button class="refresh-btn" id="refreshBtn" onclick="refresh()">更新</button>
  </div>
</div>

<div class="grid">
  <!-- GPU -->
  <div class="card" id="gpuCard">
    <h2>GPU</h2>
    <div id="gpuContent">読み込み中...</div>
  </div>

  <!-- CPU / RAM -->
  <div class="card" id="cpuCard">
    <h2>CPU / メモリ</h2>
    <div id="cpuContent">読み込み中...</div>
  </div>

  <!-- ディスク -->
  <div class="card" id="diskCard">
    <h2>ストレージ</h2>
    <div id="diskContent">読み込み中...</div>
  </div>

  <!-- AIサービス -->
  <div class="card" id="svcCard">
    <h2>AI サービス</h2>
    <div id="svcContent">読み込み中...</div>
  </div>

  <!-- デーモン -->
  <div class="card" id="daemonCard">
    <h2>デーモン心拍</h2>
    <div id="daemonContent">読み込み中...</div>
  </div>

  <!-- Watchdog -->
  <div class="card" id="wdCard">
    <h2>Watchdog / Failover</h2>
    <div id="wdContent">読み込み中...</div>
  </div>
</div>

<!-- 管理者俯瞰ビュー (拡張) -->
<div class="card" style="border-left: 4px solid var(--red);">
  <h2>異常キュー (Anomaly Queue)</h2>
  <div style="margin:8px 0; display:flex; gap:8px; flex-wrap:wrap">
    <button class="refresh-btn" onclick="runAction('audit', '監査実行中...')" style="background:var(--purple)">手動監査</button>
    <button class="refresh-btn" onclick="runAction('heal', '自動修復中...')" style="background:var(--green)">自動修復</button>
    <button class="refresh-btn" onclick="runAction('clear-queue', 'キュークリア中...')" style="background:var(--yellow);color:#000">キュークリア</button>
  </div>
  <div id="anomalyContent">読み込み中...</div>
  <div id="actionResult" style="margin-top:8px; font-size:0.8em; color:var(--dim); white-space:pre-wrap; max-height:200px; overflow-y:auto"></div>
</div>

<div class="grid">
  <div class="card">
    <h2>部門RAG状態</h2>
    <div id="deptRagContent">読み込み中...</div>
  </div>

  <div class="card">
    <h2>自律承認モード</h2>
    <div id="autoModeContent">読み込み中...</div>
  </div>

  <div class="card">
    <h2>Critical Files Guard</h2>
    <div id="guardContent">読み込み中...</div>
  </div>
</div>

<div class="card">
  <h2>スケジュールタスク (全21件)</h2>
  <div id="tasksContent">読み込み中...</div>
</div>

<!-- 監査レポート -->
<div class="card">
  <h2>System Audit</h2>
  <div id="auditContent">読み込み中...</div>
</div>

<!-- 会話ログ -->
<div class="card">
  <h2>会話ログ</h2>
  <div class="log-list" id="logList">読み込み中...</div>
</div>

<!-- モーダル -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-header">
      <span id="modalTitle">会話ログ</span>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modalBody"></div>
  </div>
</div>

<script>
let autoTimer = null;

function badge(ok, labels) {
  if (ok === true) return `<span class="badge badge-ok">${labels?.[0] || 'OK'}</span>`;
  if (ok === 'warn') return `<span class="badge badge-warn">${labels?.[1] || 'WARN'}</span>`;
  return `<span class="badge badge-err">${labels?.[2] || 'DOWN'}</span>`;
}

function bar(pct, thresholds) {
  const [w, d] = thresholds || [80, 95];
  const cls = pct >= d ? 'bar-red' : pct >= w ? 'bar-yellow' : 'bar-green';
  return `<div class="bar-bg"><div class="bar-fill ${cls}" style="width:${Math.min(pct,100)}%"></div><span class="bar-label">${pct}%</span></div>`;
}

function renderGPU(gpus, age) {
  if (!gpus || !gpus.length) return '<p style="color:var(--dim)">データなし</p>';
  let h = `<p style="font-size:0.75em;color:var(--dim)">最終更新: ${age}</p>`;
  gpus.forEach((g, i) => {
    const tempBadge = g.temp_c >= 90 ? badge(false) : g.temp_c >= 80 ? badge('warn') : badge(true, [g.temp_c+'°C']);
    h += `<h3 style="font-size:0.9em;margin:8px 0 4px">GPU${i}: ${g.name}</h3>`;
    h += `<table>`;
    h += `<tr><td>温度</td><td>${g.temp_c}°C ${tempBadge}</td></tr>`;
    h += `<tr><td>VRAM</td><td>${bar(g.vram_usage_pct, [80,95])} ${g.vram_used_mb}/${g.vram_total_mb} MB</td></tr>`;
    h += `<tr><td>稼働率</td><td>${bar(g.gpu_util_pct, [80,95])}</td></tr>`;
    h += `<tr><td>電力</td><td>${g.power_w} W</td></tr>`;
    h += `<tr><td>ファン</td><td>${g.fan_pct}%</td></tr>`;
    h += `</table>`;
  });
  return h;
}

function renderCPU(cpu, age) {
  if (!cpu) return '<p style="color:var(--dim)">データなし</p>';
  let h = '';
  h += `<table>`;
  h += `<tr><td>CPU使用率</td><td>${bar(cpu.usage_pct, [80,95])}</td></tr>`;
  if (cpu.temp_c) h += `<tr><td>CPU温度</td><td>${cpu.temp_c}°C</td></tr>`;
  h += `<tr><td>RAM</td><td>${bar(cpu.ram_pct, [85,95])} ${(cpu.ram_used_mb/1024).toFixed(1)}/${(cpu.ram_total_mb/1024).toFixed(1)} GB</td></tr>`;
  h += `</table>`;
  return h;
}

function renderDisk(disks) {
  if (!disks || !disks.length) return '<p style="color:var(--dim)">データなし</p>';
  let h = '<table><tr><th>ドライブ</th><th>使用状況</th><th>空き</th></tr>';
  disks.forEach(d => {
    h += `<tr><td>${d.drive}</td><td>${bar(d.usage_pct, [80,95])} ${d.used_gb}/${d.total_gb} GB</td><td>${d.free_gb} GB</td></tr>`;
  });
  h += '</table>';
  return h;
}

function renderServices(data) {
  const svc = data.services || {};
  const names = {claude_code:'Claude Code', codex:'Codex', ollama:'Ollama', qdrant:'Qdrant', health_server:'Health Server'};
  let h = `<p style="font-size:0.8em">管理者: <b>${data.active_manager||'不明'}</b> (${data.age})</p>`;
  h += '<table><tr><th>サービス</th><th>状態</th></tr>';
  for (const [k, v] of Object.entries(svc)) {
    h += `<tr><td>${names[k]||k}</td><td>${badge(v)}</td></tr>`;
  }
  h += '</table>';
  return h;
}

function renderDaemons(daemons) {
  if (!daemons || !daemons.length) return '<p style="color:var(--dim)">データなし</p>';
  let h = '<table><tr><th>デーモン</th><th>状態</th><th>PID</th><th>最終応答</th></tr>';
  daemons.forEach(d => {
    const st = d.status === 'alive' ? badge(true, ['稼働']) : d.status === 'stale' ? badge('warn', [0,'遅延']) : badge(false, [0,0,'停止']);
    h += `<tr><td>${d.name}</td><td>${st}</td><td>${d.pid||'-'}</td><td>${d.age}</td></tr>`;
  });
  h += '</table>';
  return h;
}

function renderWatchdog(wd) {
  let h = '<table>';
  h += `<tr><td>CLI稼働</td><td>${badge(wd.cli_running)}</td></tr>`;
  h += `<tr><td>CLI最終検出</td><td>${wd.last_cli_age}</td></tr>`;
  h += `<tr><td>最終チェック</td><td>${wd.last_check_age}</td></tr>`;
  h += `<tr><td>アラート件数</td><td>${wd.alert_count}</td></tr>`;
  h += '</table>';
  if (wd.recent_alerts && wd.recent_alerts.length) {
    h += '<h3 style="font-size:0.85em;margin-top:8px">直近アラート</h3>';
    wd.recent_alerts.forEach(a => {
      h += `<div class="alert-item">[${a.time}] ${a.message}</div>`;
    });
  }
  return h;
}

function renderLogs(logs) {
  if (!logs || !logs.length) return '<p style="color:var(--dim)">ログなし</p>';
  let h = '';
  logs.forEach(l => {
    h += `<div class="log-item" onclick="openLog('${l.name}')"><span>${l.name}</span><span style="color:var(--dim)">${l.size_kb} KB / ${l.mtime}</span></div>`;
  });
  return h;
}

function renderAnomalyQueue(q) {
  if (!q) return '<p style="color:var(--dim)">データなし</p>';
  if (q.pending_count === 0) {
    return `<p style="color:var(--green)">✓ 未対応異常なし (累計対応済: ${q.resolved_count}件)</p>`;
  }
  let h = `<p><strong style="color:var(--red)">${q.pending_count}件</strong>の未対応異常 (累計対応済: ${q.resolved_count}件)</p>`;
  h += '<table style="margin-top:8px"><tr><th>重大度</th><th>コンポーネント</th><th>メッセージ</th></tr>';
  q.pending.forEach(f => {
    const cls = f.severity === 'CRITICAL' ? 'badge-err' : f.severity === 'HIGH' ? 'badge-err' : 'badge-warn';
    h += `<tr><td><span class="badge ${cls}">${f.severity}</span></td><td>${f.component||''}</td><td style="font-size:0.85em">${(f.message||'').substring(0,80)}</td></tr>`;
  });
  h += '</table>';
  return h;
}

function renderDeptRag(rag) {
  if (!rag) return '<p style="color:var(--dim)">データなし</p>';
  let h = '<table><tr><th>部門</th><th>ポイント数</th></tr>';
  const order = ['mem0_shared', 'dept_hr', 'dept_research', 'dept_design', 'dept_build', 'dept_qa'];
  order.forEach(d => {
    const count = rag[d];
    const display = count === null || count === undefined ? '<span class="badge badge-err">ERR</span>' : count.toLocaleString();
    h += `<tr><td>${d}</td><td style="text-align:right">${display}</td></tr>`;
  });
  h += '</table>';
  return h;
}

function renderAutoMode(mode) {
  if (!mode) return '<p style="color:var(--dim)">データなし</p>';
  const statusBadge = mode.active
    ? '<span class="badge badge-warn">🔓 自律モード有効</span>'
    : '<span class="badge badge-ok">通常モード</span>';
  let h = `<p>${statusBadge}</p>`;
  h += '<table>';
  h += `<tr><td>承認待ち開始</td><td>${mode.pending_since ? new Date(mode.pending_since).toLocaleTimeString('ja-JP') : '-'}</td></tr>`;
  if (mode.active && mode.unlock_until) {
    const until = new Date(mode.unlock_until);
    const remaining = Math.max(0, (until - new Date()) / 60000);
    h += `<tr><td>解除予定</td><td>${until.toLocaleTimeString('ja-JP')} (残${remaining.toFixed(0)}分)</td></tr>`;
  }
  h += `<tr><td>累計承認</td><td>${mode.approved}件</td></tr>`;
  h += `<tr><td>累計拒否</td><td>${mode.denied}件</td></tr>`;
  if (mode.last_user_reply) {
    h += `<tr><td>最終ユーザー返信</td><td>${new Date(mode.last_user_reply).toLocaleTimeString('ja-JP')}</td></tr>`;
  }
  h += '</table>';
  return h;
}

function renderGuard(g) {
  if (!g) return '<p style="color:var(--dim)">データなし</p>';
  let h = `<p>保護中: <strong>${g.tracked_files}</strong>ファイル</p>`;
  if (g.last_run) {
    h += `<p style="font-size:0.75em;color:var(--dim)">最終チェック: ${new Date(g.last_run).toLocaleString('ja-JP')}</p>`;
  }
  if (g.recent_changes && g.recent_changes.length) {
    h += '<h3 style="font-size:0.85em;margin-top:8px">最近の変更</h3>';
    g.recent_changes.slice(0, 5).forEach(c => {
      const cls = c.event === 'auto_restored' ? 'badge-err' : c.event === 'changed' ? 'badge-warn' : 'badge-off';
      h += `<div style="font-size:0.75em;padding:2px 0"><span class="badge ${cls}">${c.event}</span> ${c.path}</div>`;
    });
  }
  return h;
}

function renderTasks(tasks) {
  if (!tasks || !tasks.length) return '<p style="color:var(--dim)">データなし</p>';
  let h = '<table><tr><th>タスク名</th><th>状態</th><th>最終実行</th><th>経過</th></tr>';
  tasks.forEach(t => {
    let statusBadge;
    if (t.status === 'ok') statusBadge = '<span class="badge badge-ok">OK</span>';
    else if (t.status === 'never_run') statusBadge = '<span class="badge badge-warn">未実行</span>';
    else if (t.status === 'missing') statusBadge = '<span class="badge badge-err">MISSING</span>';
    else if (t.status === 'failed') statusBadge = `<span class="badge badge-err">FAIL(${t.result})</span>`;
    else statusBadge = `<span class="badge badge-off">${t.status}</span>`;

    const ageDisplay = t.age_min !== undefined
      ? (t.age_min < 60 ? t.age_min + '分' : Math.floor(t.age_min/60) + '時間' + (t.age_min%60) + '分')
      : '-';
    h += `<tr><td>${t.name}</td><td>${statusBadge}</td><td style="font-size:0.8em">${t.last_run||'-'}</td><td style="font-size:0.8em">${ageDisplay}</td></tr>`;
  });
  h += '</table>';
  return h;
}

function renderAudit(audit) {
  if (!audit || !audit.available) return '<p style="color:var(--dim)">監査未実行</p>';
  const s = audit.summary || {};
  let h = `<p style="font-size:0.75em;color:var(--dim)">最終監査: ${audit.timestamp_jst}</p>`;
  h += `<table><tr><th>CRITICAL</th><th>HIGH</th><th>MEDIUM</th><th>LOW</th><th>改善提案</th></tr>`;
  h += `<tr><td>${s.critical ? '<span class="badge badge-err">'+s.critical+'</span>' : '0'}</td>`;
  h += `<td>${s.high ? '<span class="badge badge-warn">'+s.high+'</span>' : '0'}</td>`;
  h += `<td>${s.medium||0}</td><td>${s.low||0}</td><td>${s.improvements||0}</td></tr></table>`;
  if (audit.findings && audit.findings.length) {
    h += '<h3 style="font-size:0.85em;margin-top:8px">検出事項</h3>';
    audit.findings.forEach(f => {
      const cls = f.severity === 'CRITICAL' || f.severity === 'HIGH' ? 'badge-err' : f.severity === 'MEDIUM' ? 'badge-warn' : 'badge-off';
      h += `<div style="font-size:0.8em;padding:2px 0"><span class="badge ${cls}">${f.severity}</span> ${f.component}: ${f.message.substring(0,60)}</div>`;
    });
  }
  if (audit.improvements && audit.improvements.length) {
    h += '<h3 style="font-size:0.85em;margin-top:8px;color:var(--accent)">改善提案</h3>';
    audit.improvements.forEach(imp => {
      h += `<div style="font-size:0.8em;padding:2px 0;color:var(--accent)">[${imp.priority}] ${(imp.message||'').substring(0,70)}</div>`;
    });
  }
  return h;
}

async function refresh() {
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true; btn.textContent = '更新中...';
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    document.getElementById('updateTime').textContent = data.collected_at_jst;
    document.getElementById('gpuContent').innerHTML = renderGPU(data.hw?.gpus, data.hw?.age);
    document.getElementById('cpuContent').innerHTML = renderCPU(data.hw?.cpu, data.hw?.age);
    document.getElementById('diskContent').innerHTML = renderDisk(data.hw?.disk);
    document.getElementById('svcContent').innerHTML = renderServices(data.services);
    document.getElementById('daemonContent').innerHTML = renderDaemons(data.daemons);
    document.getElementById('wdContent').innerHTML = renderWatchdog(data.watchdog);
    document.getElementById('auditContent').innerHTML = renderAudit(data.audit);
    document.getElementById('logList').innerHTML = renderLogs(data.conversation_logs);
    // 拡張: 管理者俯瞰ビュー
    document.getElementById('anomalyContent').innerHTML = renderAnomalyQueue(data.anomaly_queue);
    document.getElementById('deptRagContent').innerHTML = renderDeptRag(data.dept_rag);
    document.getElementById('autoModeContent').innerHTML = renderAutoMode(data.auto_mode);
    document.getElementById('guardContent').innerHTML = renderGuard(data.critical_guard);
    document.getElementById('tasksContent').innerHTML = renderTasks(data.scheduled_tasks);
  } catch(e) {
    document.getElementById('updateTime').textContent = 'エラー: ' + e.message;
  }
  btn.disabled = false; btn.textContent = '更新';
}

async function runAction(action, loadingMsg) {
  const result = document.getElementById('actionResult');
  result.textContent = loadingMsg;
  result.style.color = 'var(--accent)';
  try {
    const res = await fetch('/api/' + action, { method: 'POST' });
    const data = await res.json();
    result.style.color = data.success ? 'var(--green)' : 'var(--red)';
    let msg = data.success ? '[成功] ' : '[失敗] ';
    if (data.output) msg += '\n' + data.output;
    if (data.error) msg += '\n[エラー] ' + data.error;
    result.textContent = msg;
    // 成功後にリフレッシュ
    if (data.success) setTimeout(refresh, 1000);
  } catch(e) {
    result.style.color = 'var(--red)';
    result.textContent = 'エラー: ' + e.message;
  }
}

async function openLog(name) {
  const modal = document.getElementById('modal');
  document.getElementById('modalTitle').textContent = name;
  document.getElementById('modalBody').textContent = '読み込み中...';
  modal.classList.add('active');
  try {
    const res = await fetch('/api/log?name=' + encodeURIComponent(name));
    document.getElementById('modalBody').textContent = await res.text();
  } catch(e) {
    document.getElementById('modalBody').textContent = 'エラー: ' + e.message;
  }
}

function closeModal() {
  document.getElementById('modal').classList.remove('active');
}

document.getElementById('modal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

document.getElementById('autoRefresh').addEventListener('change', function() {
  if (this.checked) {
    autoTimer = setInterval(refresh, 30000);
  } else {
    clearInterval(autoTimer); autoTimer = null;
  }
});

refresh();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP サーバー
# ---------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_html(DASHBOARD_HTML)
        elif path == "/api/status":
            self._send_json(collect_all())
        elif path == "/api/log":
            params = parse_qs(parsed.query)
            name = params.get("name", [""])[0]
            content = read_conversation_log(name)
            self._send_text(content if content else "ファイルが見つかりません")
        else:
            self.send_error(404)

    def do_POST(self):
        """POSTエンドポイント: self-heal等のアクション実行."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/heal":
            # 自動修復をトリガー
            result = run_self_heal_action()
            self._send_json(result)
        elif path == "/api/audit":
            # 手動監査実行
            result = run_audit_action()
            self._send_json(result)
        elif path == "/api/clear-queue":
            # 異常キューをクリア
            result = clear_anomaly_queue()
            self._send_json(result)
        else:
            self.send_error(404)

    def _send_html(self, content: str):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text: str):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass  # アクセスログを抑制


def main():
    port = PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # localhost のみバインド（セキュリティ）
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Helix Dashboard: http://localhost:{port}")
    print(f"バインド: 127.0.0.1:{port} (ローカルのみ)")
    print("Ctrl+C で停止")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
        server.server_close()


if __name__ == "__main__":
    main()
