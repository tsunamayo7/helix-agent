"""Helix Overview — Claude専用の統合俯瞰ツール.

このツールはClaudeが環境全体を1コマンドで把握するためのCLIサマリー。
dashboard_server.py(ユーザー向けブラウザダッシュボード)とは別に、テキストベースで
全システムを俯瞰できるよう設計されている。

対象9領域:
  1. Helix Corp (会社経営フロー)
  2. 記憶システム (mem0/cmem/LightRAG/memory/)
  3. RAG成長 (DeptFeed/x-feed-collector)
  4. 異常検知 (anomaly_queue/audit/drift)
  5. Claude設定 (CLAUDE.md/settings.json/hooks)
  6. 起動システム (bat/tasks/services)
  7. 生成物/ツール管理 (tools/apps)
  8. セキュリティ (hooks/guards)
  9. 保守機構 (auditor/supervisor)

使い方:
    python scripts/helix_overview.py                  # 全体サマリー
    python scripts/helix_overview.py --full           # 詳細情報
    python scripts/helix_overview.py --json           # JSON出力
    python scripts/helix_overview.py --section N     # 特定領域のみ (番号 or キーワード)
    python scripts/helix_overview.py --problems      # 問題のあるセクションのみ
    python scripts/helix_overview.py --docs          # 関連ドキュメントを表示

自然言語対応 (--section で使える):
    "記憶"/"memory"/"mem" → [2]
    "会社"/"部門"/"corp"/"dept" → [1]
    "成長"/"rag"/"feed" → [3]
    "異常"/"エラー"/"anomaly"/"audit" → [4]
    "設定"/"config"/"claude_md" → [5]
    "起動"/"サービス"/"startup"/"service" → [6]
    "ツール"/"プロジェクト"/"project"/"tool" → [7]
    "セキュリティ"/"security"/"hook" → [8]
    "保守"/"maintenance"/"daemon" → [9]
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

if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------

HELIX_DIR = Path.home() / ".helix-agent"
CLAUDE_DIR = Path.home() / ".claude"
HOOKS_DIR = CLAUDE_DIR / "hooks"
SKILLS_DIR = CLAUDE_DIR / "skills"
MEMORY_DIR = CLAUDE_DIR / "projects" / "C--Development" / "memory"
DEV_DIR = Path("C:/Development")
TOOLS_DIR = DEV_DIR / "tools"
APPS_DIR = DEV_DIR / "apps"

CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"
SETTINGS_JSON = CLAUDE_DIR / "settings.json"
MEMORY_MD = MEMORY_DIR / "MEMORY.md"

QDRANT_URL = "http://localhost:6333"

# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _http_get(url: str, timeout: int = 3) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _http_post(url: str, body: dict, timeout: int = 3) -> dict | None:
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _age_str(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        if age < 60:
            return f"{int(age)}s ago"
        if age < 3600:
            return f"{int(age/60)}m ago"
        if age < 86400:
            return f"{int(age/3600)}h ago"
        return f"{int(age/86400)}d ago"
    except Exception:
        return iso[:16]


def _file_size_human(p: Path) -> str:
    if not p.exists():
        return "N/A"
    try:
        s = p.stat().st_size
        if s < 1024:
            return f"{s}B"
        if s < 1024**2:
            return f"{s/1024:.1f}KB"
        return f"{s/1024**2:.1f}MB"
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# 領域1: Helix Corp (会社経営フロー)
# ---------------------------------------------------------------------------


def collect_corp() -> dict:
    data = {"departments": {}, "skills": [], "workflow_dag": None}

    # 部門RAGポイント数
    depts = ["dept_hr", "dept_research", "dept_design", "dept_build", "dept_qa"]
    for d in depts:
        info = _http_get(f"{QDRANT_URL}/collections/{d}")
        if info:
            data["departments"][d] = info.get("result", {}).get("points_count", 0)
        else:
            data["departments"][d] = None

    # Skills (corp-*/dept-*)
    if SKILLS_DIR.exists():
        for skill_dir in SKILLS_DIR.iterdir():
            if skill_dir.is_dir() and (skill_dir.name.startswith("corp-") or skill_dir.name.startswith("dept-")):
                skill_md = skill_dir / "SKILL.md"
                data["skills"].append({
                    "name": skill_dir.name,
                    "exists": skill_md.exists(),
                    "size": _file_size_human(skill_md),
                })

    # WorkflowDAG
    dag_path = Path("C:/Development/tools/helix-agent/src/workflow_dag.py")
    data["workflow_dag"] = {"exists": dag_path.exists(), "size": _file_size_human(dag_path)}

    return data


# ---------------------------------------------------------------------------
# 領域2: 記憶システム
# ---------------------------------------------------------------------------


def collect_memory() -> dict:
    data = {
        "qdrant": {},
        "memory_files": {},
        "cmem_db": {},
        "lightrag": {},
    }

    # Qdrantコレクション
    cols = _http_get(f"{QDRANT_URL}/collections")
    if cols:
        for c in cols.get("result", {}).get("collections", []):
            name = c["name"]
            info = _http_get(f"{QDRANT_URL}/collections/{name}")
            if info:
                data["qdrant"][name] = info.get("result", {}).get("points_count", 0)

    # memory/ディレクトリ
    if MEMORY_DIR.exists():
        md_files = list(MEMORY_DIR.glob("*.md"))
        data["memory_files"] = {
            "total": len(md_files),
            "memory_md_exists": MEMORY_MD.exists(),
            "memory_md_size": _file_size_human(MEMORY_MD),
            "archive_count": len(list((MEMORY_DIR / "archive").glob("*.md"))) if (MEMORY_DIR / "archive").exists() else 0,
        }

    # cmem DB
    cmem_db = Path.home() / ".claude-mem" / "claude-mem.db"
    if cmem_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(cmem_db))
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM observations")
            data["cmem_db"] = {
                "exists": True,
                "observations": cur.fetchone()[0],
                "size": _file_size_human(cmem_db),
            }
            conn.close()
        except Exception as e:
            data["cmem_db"] = {"exists": True, "error": str(e)}
    else:
        data["cmem_db"] = {"exists": False}

    # LightRAG
    health = _http_get("http://127.0.0.1:9621/health")
    if health:
        data["lightrag"] = {
            "status": health.get("status", "?"),
            "pipeline_busy": health.get("pipeline_busy"),
        }
    else:
        data["lightrag"] = {"status": "DOWN"}

    return data


# ---------------------------------------------------------------------------
# 領域3: RAG成長 (DeptFeed/x-feed-collector)
# ---------------------------------------------------------------------------


def collect_rag_growth() -> dict:
    data = {"dept_feed": {}, "x_feed_collector": {}}

    # DeptFeed state
    state = _read_json(HELIX_DIR / "dept_feed" / "state.json")
    if state:
        data["dept_feed"] = {
            "last_run": state.get("last_run"),
            "last_run_age": _age_str(state.get("last_run")),
            "synced_hashes": len(state.get("synced_hashes", [])),
            "last_stats": state.get("stats", {}),
        }

    # x-feed-collector DB (feeds.db)
    feeds_db = Path("C:/Development/tools/x-feed-collector/data/feeds.db")
    if feeds_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(feeds_db))
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM entries")
            total = cur.fetchone()[0]
            cur.execute("SELECT MAX(created_at) FROM entries")
            last = cur.fetchone()[0]
            data["x_feed_collector"] = {
                "total_entries": total,
                "last_entry": last,
                "last_entry_age": _age_str(last) if last else "never",
                "db_size": _file_size_human(feeds_db),
            }
            conn.close()
        except Exception as e:
            data["x_feed_collector"] = {"error": str(e)}

    return data


# ---------------------------------------------------------------------------
# 領域4: 異常検知
# ---------------------------------------------------------------------------


def collect_anomalies() -> dict:
    data = {"queue": {}, "audit_report": {}, "drift": {}}

    # Anomaly queue
    q = _read_json(HELIX_DIR / "anomaly_queue.json")
    if q:
        data["queue"] = {
            "pending_count": len(q.get("pending", [])),
            "resolved_count": len(q.get("resolved", [])),
            "last_updated": q.get("last_updated"),
            "pending_items": [
                {
                    "severity": p.get("severity"),
                    "component": p.get("component"),
                    "message": p.get("message", "")[:100],
                }
                for p in q.get("pending", [])[:10]
            ],
        }

    # Latest audit report
    report = _read_json(HELIX_DIR / "audit_reports" / "latest.json")
    if report:
        findings = report.get("findings", [])
        by_sev = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for f in findings:
            sev = f.get("severity", "").upper()
            if sev in by_sev:
                by_sev[sev] += 1
        data["audit_report"] = {
            "timestamp": report.get("timestamp"),
            "age": _age_str(report.get("timestamp")),
            "total_findings": len(findings),
            "by_severity": by_sev,
        }

    # Drift log
    drift_log = HELIX_DIR / "drift_log.jsonl"
    if drift_log.exists():
        try:
            lines = drift_log.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                last = json.loads(lines[-1])
                data["drift"] = {"last_check": last.get("timestamp"), "total_checks": len(lines)}
        except Exception:
            pass

    return data


# ---------------------------------------------------------------------------
# 領域5: Claude設定
# ---------------------------------------------------------------------------


def collect_claude_config() -> dict:
    data = {
        "claude_md": {"exists": False},
        "settings_json": {"exists": False},
        "hooks": [],
        "statusline": {"exists": False},
    }

    # CLAUDE.md
    if CLAUDE_MD.exists():
        content = CLAUDE_MD.read_text(encoding="utf-8")
        lines = content.splitlines()
        data["claude_md"] = {
            "exists": True,
            "size": _file_size_human(CLAUDE_MD),
            "lines": len(lines),
            "section_count": sum(1 for l in lines if l.startswith("## ")),
        }

    # settings.json
    settings = _read_json(SETTINGS_JSON)
    if settings:
        hooks = settings.get("hooks", {})
        data["settings_json"] = {
            "exists": True,
            "size": _file_size_human(SETTINGS_JSON),
            "permissions_default": settings.get("permissions", {}).get("defaultMode"),
            "allow_count": len(settings.get("permissions", {}).get("allow", [])),
            "deny_count": len(settings.get("permissions", {}).get("deny", [])),
            "hook_events": list(hooks.keys()),
            "mcp_server_count": len(settings.get("mcpServers", {})),
        }

    # Hooks
    if HOOKS_DIR.exists():
        for hook in HOOKS_DIR.glob("*.py"):
            data["hooks"].append({"name": hook.name, "size": _file_size_human(hook)})

    # statusline
    statusline = CLAUDE_DIR / "statusline.py"
    data["statusline"] = {"exists": statusline.exists(), "size": _file_size_human(statusline)}

    return data


# ---------------------------------------------------------------------------
# 領域6: 起動システム
# ---------------------------------------------------------------------------


def collect_startup() -> dict:
    data = {"bat_files": {}, "services": {}, "scheduled_tasks": {}}

    # bat files
    bats = [
        ("start_claude.bat", DEV_DIR / "start" / "manual" / "start_claude.bat"),
        ("start_all_services.bat", DEV_DIR / "start" / "auto" / "start_all_services.bat"),
        ("run_bg.vbs", DEV_DIR / "start" / "manual" / "run_bg.vbs"),
    ]
    for name, p in bats:
        data["bat_files"][name] = {"exists": p.exists(), "size": _file_size_human(p)}

    # サービス生死確認
    services = {
        "qdrant": "http://localhost:6333/collections",
        "ollama": "http://localhost:11434/api/tags",
        "qdrant_memory": "http://localhost:8080/health",
        "clip_bridge": "http://localhost:9999/health",
        "lightrag": "http://127.0.0.1:9621/health",
        "health_server": "http://localhost:8800/health",
        "dashboard": "http://localhost:8801/",  # /api/status is slow (~20s sync collect_all), root is instant
    }
    for name, url in services.items():
        if name == "dashboard":
            # Dashboard root returns HTML, not JSON — check HTTP 200 only
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    ok = resp.status == 200
            except Exception:
                ok = False
        else:
            ok = _http_get(url) is not None
        data["services"][name] = "UP" if ok else "DOWN"

    # Scheduled tasks (一括取得)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-ScheduledTask | Where-Object { $_.TaskName -match 'Helix|helix|X-Feed' } | "
             "ForEach-Object { $i = Get-ScheduledTaskInfo -TaskName $_.TaskName; "
             "Write-Output \"$($_.TaskName)|$($_.State)|$($i.LastTaskResult)|$($i.LastRunTime.ToString('o'))\" }"],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        tasks = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) == 4:
                name, state, code, last_run = parts
                tasks.append({
                    "name": name,
                    "state": state,
                    "result": code,
                    "last_run": last_run[:16] if not last_run.startswith("1999") else "never",
                })
        data["scheduled_tasks"] = {
            "total": len(tasks),
            "ok": sum(1 for t in tasks if t["result"] == "0"),
            "never_run": sum(1 for t in tasks if t["last_run"] == "never"),
            "failed": sum(1 for t in tasks if t["result"] not in ("0", "267011", "267009")),  # 267009=TASK_RUNNING
            "items": tasks,
        }
    except Exception as e:
        data["scheduled_tasks"] = {"error": str(e)}

    return data


# ---------------------------------------------------------------------------
# 領域7: 生成物/ツール管理
# ---------------------------------------------------------------------------


def collect_projects() -> dict:
    data = {"tools": [], "apps": []}

    for base, target in [(TOOLS_DIR, "tools"), (APPS_DIR, "apps")]:
        if not base.exists():
            continue
        for project in base.iterdir():
            if not project.is_dir() or project.name.startswith("."):
                continue
            info = {"name": project.name}

            # pyproject.toml
            pyproject = project / "pyproject.toml"
            if pyproject.exists():
                try:
                    content = pyproject.read_text(encoding="utf-8")
                    import re
                    ver_match = re.search(r'version\s*=\s*"([^"]+)"', content)
                    name_match = re.search(r'name\s*=\s*"([^"]+)"', content)
                    info["version"] = ver_match.group(1) if ver_match else "?"
                    info["pyproject_name"] = name_match.group(1) if name_match else "?"
                except Exception:
                    pass

            # package.json
            pkg = project / "package.json"
            if pkg.exists():
                pkg_data = _read_json(pkg)
                if pkg_data:
                    info["package_version"] = pkg_data.get("version", "?")

            # git
            git_dir = project / ".git"
            info["has_git"] = git_dir.exists()

            data[target].append(info)

    return data


# ---------------------------------------------------------------------------
# 領域8: セキュリティ
# ---------------------------------------------------------------------------


def collect_security() -> dict:
    data = {"hooks": {}, "guards": {}, "auto_mode": {}}

    # Hooks
    security_hooks = [
        "pretool_security.py",
        "smart_approval.py",
        "timeout_auto_approve.py",
    ]
    for h in security_hooks:
        p = HOOKS_DIR / h
        data["hooks"][h] = {"exists": p.exists(), "size": _file_size_human(p)}

    # Smart approval rules
    rules_file = HELIX_DIR / "approval_rules" / "rules.json"
    if rules_file.exists():
        rules_data = _read_json(rules_file)
        if rules_data:
            data["hooks"]["smart_approval_rules"] = {
                "version": rules_data.get("version"),
                "rule_count": len(rules_data.get("rules", [])),
            }

    # Audit log size
    audit_log = HELIX_DIR / "approval_rules" / "audit.jsonl"
    if audit_log.exists():
        data["hooks"]["audit_log_size"] = _file_size_human(audit_log)

    # Critical files guard
    guard_state = _read_json(HELIX_DIR / "critical_guard" / "state.json")
    if guard_state:
        data["guards"]["critical_files"] = {
            "tracked": len(guard_state.get("files", {})),
            "last_run": guard_state.get("last_run"),
            "last_run_age": _age_str(guard_state.get("last_run")),
        }

    # Auto mode state
    auto = _read_json(HELIX_DIR / "auto_mode" / "state.json")
    if auto:
        unlock = auto.get("unlock_until")
        active = False
        if unlock:
            try:
                until = datetime.fromisoformat(unlock)
                active = datetime.now(timezone.utc) < until
            except Exception:
                pass
        data["auto_mode"] = {
            "active": active,
            "pending_since": auto.get("pending_since"),
            "approved": auto.get("auto_approved_count", 0),
            "denied": auto.get("auto_denied_count", 0),
        }

    return data


# ---------------------------------------------------------------------------
# 領域9: 保守機構
# ---------------------------------------------------------------------------


def collect_maintenance() -> dict:
    data = {"daemons": {}, "scripts": {}, "backups": {}}

    # Daemon heartbeats
    hb_dir = HELIX_DIR / "heartbeats"
    if hb_dir.exists():
        for hb in hb_dir.glob("*.json"):
            hb_data = _read_json(hb)
            if hb_data:
                data["daemons"][hb.stem] = {
                    "status": hb_data.get("status", "?"),
                    "timestamp": hb_data.get("timestamp"),
                    "age": _age_str(hb_data.get("timestamp")),
                    "pid": hb_data.get("pid"),
                }

    # Maintenance scripts
    scripts_dir = Path("C:/Development/tools/helix-agent/scripts")
    if scripts_dir.exists():
        maintenance_scripts = [
            "system_auditor.py",
            "integrity_check.py",
            "memory_health.py",
            "contradiction_detector.py",
            "qdrant_dedup.py",
            "backup_to_nas.py",
            "supervisor.py",
            "watchdog.py",
            "failover_orchestrator.py",
            "escalation.py",
            "anomaly_dispatcher.py",
            "critical_files_guard.py",
        ]
        for s in maintenance_scripts:
            p = scripts_dir / s
            data["scripts"][s] = {"exists": p.exists(), "size": _file_size_human(p)}

    # Backups (_backup_*)
    if MEMORY_DIR.exists():
        backups = sorted(MEMORY_DIR.glob("_backup_*"), reverse=True)
        data["backups"]["count"] = len(backups)
        if backups:
            latest = backups[0]
            age_sec = datetime.now().timestamp() - latest.stat().st_mtime
            data["backups"]["latest"] = latest.name
            data["backups"]["latest_age_hours"] = int(age_sec / 3600)

    return data


# ---------------------------------------------------------------------------
# メインレンダラー
# ---------------------------------------------------------------------------


def collect_all() -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sections": {
            "1_corp": collect_corp(),
            "2_memory": collect_memory(),
            "3_rag_growth": collect_rag_growth(),
            "4_anomalies": collect_anomalies(),
            "5_claude_config": collect_claude_config(),
            "6_startup": collect_startup(),
            "7_projects": collect_projects(),
            "8_security": collect_security(),
            "9_maintenance": collect_maintenance(),
        },
    }


def render_summary(data: dict, full: bool = False) -> str:
    lines = []
    s = data["sections"]
    ts = data["timestamp"][:19]

    lines.append("=" * 70)
    lines.append(f"  Helix Overview — {ts}")
    lines.append("=" * 70)

    # 1. Corp
    corp = s["1_corp"]
    lines.append("\n[1] Helix Corp (会社経営フロー)")
    total_dept = sum(v for v in corp["departments"].values() if v is not None)
    lines.append(f"  部門RAG合計: {total_dept} points")
    for name, count in corp["departments"].items():
        display = f"{count} pts" if count is not None else "ERR"
        lines.append(f"    {name}: {display}")
    lines.append(f"  Skills: {len(corp['skills'])}件")
    if full:
        for sk in corp["skills"]:
            lines.append(f"    {sk['name']} ({sk['size']})")

    # 2. Memory
    mem = s["2_memory"]
    lines.append("\n[2] 記憶システム")
    qd_total = sum(v for v in mem["qdrant"].values() if v is not None)
    lines.append(f"  Qdrant: {len(mem['qdrant'])} collections, {qd_total} points total")
    mf = mem["memory_files"]
    lines.append(f"  memory/: {mf.get('total', '?')}ファイル (archive: {mf.get('archive_count', '?')}件)")
    cmem = mem["cmem_db"]
    if cmem.get("exists"):
        lines.append(f"  cmem DB: {cmem.get('observations', '?')} observations ({cmem.get('size', '?')})")
    lr = mem["lightrag"]
    lines.append(f"  LightRAG: {lr.get('status', '?')}")

    # 3. RAG Growth
    rg = s["3_rag_growth"]
    lines.append("\n[3] RAG成長")
    df = rg["dept_feed"]
    if df:
        lines.append(f"  DeptFeed: 最終実行 {df.get('last_run_age', '?')}, 同期済 {df.get('synced_hashes', 0)}件")
    xfc = rg["x_feed_collector"]
    if xfc:
        lines.append(f"  x-feed-collector: {xfc.get('total_entries', '?')}エントリ, 最終 {xfc.get('last_entry_age', '?')}")

    # 4. Anomalies
    an = s["4_anomalies"]
    lines.append("\n[4] 異常検知")
    q = an["queue"]
    if q:
        status = "✓" if q.get("pending_count", 0) == 0 else "!!"
        lines.append(f"  {status} Queue: {q.get('pending_count', 0)} pending / {q.get('resolved_count', 0)} resolved")
        if full and q.get("pending_items"):
            for item in q["pending_items"]:
                lines.append(f"    [{item['severity']}] {item['component']}: {item['message'][:70]}")
    ar = an["audit_report"]
    if ar:
        sev = ar.get("by_severity", {})
        lines.append(f"  Audit: CRIT={sev.get('CRITICAL', 0)} HIGH={sev.get('HIGH', 0)} MED={sev.get('MEDIUM', 0)} LOW={sev.get('LOW', 0)} ({ar.get('age', '?')})")

    # 5. Claude config
    cc = s["5_claude_config"]
    lines.append("\n[5] Claude設定")
    cmd = cc["claude_md"]
    if cmd.get("exists"):
        lines.append(f"  CLAUDE.md: {cmd.get('lines', '?')}行, {cmd.get('section_count', '?')}セクション ({cmd.get('size', '?')})")
    sj = cc["settings_json"]
    if sj.get("exists"):
        lines.append(f"  settings.json: mode={sj.get('permissions_default', '?')}, allow={sj.get('allow_count', '?')}, deny={sj.get('deny_count', '?')}")
        lines.append(f"    hook events: {', '.join(sj.get('hook_events', []))}")
        lines.append(f"    MCP servers: {sj.get('mcp_server_count', '?')}")
    lines.append(f"  Hooks: {len(cc['hooks'])}件")

    # 6. Startup
    st = s["6_startup"]
    lines.append("\n[6] 起動システム")
    up_count = sum(1 for v in st["services"].values() if v == "UP")
    total_svc = len(st["services"])
    lines.append(f"  サービス: {up_count}/{total_svc} UP")
    for name, status in st["services"].items():
        mark = "✓" if status == "UP" else "✗"
        lines.append(f"    {mark} {name}: {status}")
    sched = st["scheduled_tasks"]
    if isinstance(sched, dict) and "total" in sched:
        lines.append(f"  Tasks: {sched['ok']}/{sched['total']} OK, {sched.get('never_run', 0)} never-run, {sched.get('failed', 0)} failed")

    # 7. Projects
    proj = s["7_projects"]
    lines.append("\n[7] 生成物/ツール管理")
    lines.append(f"  tools/: {len(proj['tools'])}プロジェクト")
    if full:
        for p in proj["tools"]:
            ver = p.get("version", p.get("package_version", "?"))
            git = " [git]" if p.get("has_git") else ""
            lines.append(f"    {p['name']} v{ver}{git}")
    lines.append(f"  apps/: {len(proj['apps'])}プロジェクト")
    if full:
        for p in proj["apps"]:
            ver = p.get("version", p.get("package_version", "?"))
            git = " [git]" if p.get("has_git") else ""
            lines.append(f"    {p['name']} v{ver}{git}")

    # 8. Security
    sec = s["8_security"]
    lines.append("\n[8] セキュリティ")
    hooks = sec["hooks"]
    lines.append(f"  Security hooks: pretool_security={('✓' if hooks.get('pretool_security.py', {}).get('exists') else '✗')}, smart_approval={('✓' if hooks.get('smart_approval.py', {}).get('exists') else '✗')}, timeout_auto={('✓' if hooks.get('timeout_auto_approve.py', {}).get('exists') else '✗')}")
    if "smart_approval_rules" in hooks:
        r = hooks["smart_approval_rules"]
        lines.append(f"  Smart approval rules: {r.get('rule_count', '?')}件")
    cg = sec["guards"].get("critical_files", {})
    if cg:
        lines.append(f"  Critical files guard: {cg.get('tracked', '?')}ファイル追跡 (最終 {cg.get('last_run_age', '?')})")
    am = sec["auto_mode"]
    if am:
        status_str = "🔓 ACTIVE" if am.get("active") else "通常"
        lines.append(f"  Auto mode: {status_str} (累計 承認{am.get('approved', 0)}/拒否{am.get('denied', 0)})")

    # 9. Maintenance
    mt = s["9_maintenance"]
    lines.append("\n[9] 保守機構")
    daemons = mt["daemons"]
    if daemons:
        alive = sum(1 for d in daemons.values() if d.get("status") == "alive")
        lines.append(f"  Daemons: {alive}/{len(daemons)} alive")
        if full:
            for name, info in daemons.items():
                lines.append(f"    {name}: {info.get('status', '?')} ({info.get('age', '?')})")
    scripts = mt["scripts"]
    missing = [n for n, v in scripts.items() if not v.get("exists")]
    lines.append(f"  Scripts: {len(scripts) - len(missing)}/{len(scripts)} 存在")
    if missing and full:
        lines.append(f"    MISSING: {', '.join(missing)}")
    bk = mt["backups"]
    if bk:
        lines.append(f"  Backups: {bk.get('count', 0)}世代, 最新 {bk.get('latest_age_hours', '?')}時間前")

    lines.append("\n" + "=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 自然言語キーワード → セクション番号マッピング
# ---------------------------------------------------------------------------

SECTION_KEYWORDS = {
    "1_corp": ["corp", "dept", "会社", "部門", "経営", "組織", "helix_corp"],
    "2_memory": ["memory", "mem", "記憶", "qdrant", "rag", "cmem", "lightrag"],
    "3_rag_growth": ["growth", "feed", "成長", "収集", "dept_feed", "x-feed"],
    "4_anomalies": ["anomaly", "anomalies", "異常", "エラー", "audit", "error", "problem"],
    "5_claude_config": ["config", "設定", "claude_md", "claude.md", "settings", "hook"],
    "6_startup": ["startup", "起動", "service", "サービス", "task", "bat", "scheduler"],
    "7_projects": ["project", "tool", "ツール", "プロジェクト", "app"],
    "8_security": ["security", "セキュリティ", "guard", "approval"],
    "9_maintenance": ["maintenance", "保守", "daemon", "supervisor", "デーモン"],
}

# セクション → 関連memoryドキュメント
SECTION_DOCS = {
    "1_corp": [
        "memory/project_helix_corp_v3.md",
        "memory/reference_helix_corp_overview.md",
        "memory/reference_helix_corp_technical.md",
    ],
    "2_memory": [
        "memory/architecture_memory_v2.md",
    ],
    "3_rag_growth": [
        "memory/project_x_feed_collector.md",
    ],
    "4_anomalies": [
        "memory/report_system_audit_20260410.md",
    ],
    "5_claude_config": [
        "~/.claude/CLAUDE.md",
        "~/.claude/settings.json",
    ],
    "6_startup": [
        "memory/infra_start_claude_bat.md",
    ],
    "7_projects": [
        "memory/project_helix_agent.md",
        "memory/project_helix_clipper.md",
    ],
    "8_security": [
        "~/.claude/hooks/smart_approval.py",
        "~/.claude/hooks/pretool_security.py",
    ],
    "9_maintenance": [
        "memory/reference_session_20260409_improvements.md",
    ],
}


def resolve_section(query: str) -> str | None:
    """自然言語クエリからセクションキーを解決."""
    q = query.lower().strip()
    # 番号指定 (1-9)
    if q in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
        mapping = {
            "1": "1_corp", "2": "2_memory", "3": "3_rag_growth",
            "4": "4_anomalies", "5": "5_claude_config", "6": "6_startup",
            "7": "7_projects", "8": "8_security", "9": "9_maintenance",
        }
        return mapping.get(q)
    # キーワードマッチ
    for section, keywords in SECTION_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return section
    return None


def extract_problems(data: dict) -> list[str]:
    """データから問題のあるポイントを抽出."""
    problems = []
    s = data["sections"]

    # 1. Corp: 部門RAGが空
    empty_depts = [n for n, v in s["1_corp"]["departments"].items() if v == 0]
    if empty_depts:
        problems.append(f"[1] 空の部門RAG: {', '.join(empty_depts)}")

    # 2. Memory: LightRAG down
    if s["2_memory"]["lightrag"].get("status") != "healthy":
        problems.append(f"[2] LightRAG: {s['2_memory']['lightrag'].get('status', '?')}")

    # 3. RAG growth: x-feed 古い
    xfc = s["3_rag_growth"].get("x_feed_collector", {})
    if "last_entry_age" in xfc and "d ago" in (xfc.get("last_entry_age") or ""):
        problems.append(f"[3] x-feed-collector: 最終エントリ {xfc['last_entry_age']}")

    # 4. Anomalies
    q = s["4_anomalies"].get("queue", {})
    if q.get("pending_count", 0) > 0:
        problems.append(f"[4] 未対応異常: {q['pending_count']}件")
    ar = s["4_anomalies"].get("audit_report", {})
    sev = ar.get("by_severity", {})
    if sev.get("CRITICAL", 0) > 0 or sev.get("HIGH", 0) > 0:
        problems.append(f"[4] Audit CRITICAL={sev.get('CRITICAL', 0)} HIGH={sev.get('HIGH', 0)}")

    # 6. Startup: service down
    down_svc = [n for n, v in s["6_startup"]["services"].items() if v == "DOWN"]
    if down_svc:
        problems.append(f"[6] サービスダウン: {', '.join(down_svc)}")

    sched = s["6_startup"]["scheduled_tasks"]
    if isinstance(sched, dict):
        if sched.get("failed", 0) > 0:
            problems.append(f"[6] 失敗タスク: {sched['failed']}件")
        if sched.get("never_run", 0) > 0:
            problems.append(f"[6] 未実行タスク: {sched['never_run']}件")

    # 9. Maintenance: daemon dead
    daemons = s["9_maintenance"].get("daemons", {})
    dead = [n for n, d in daemons.items() if d.get("status") != "alive"]
    if dead:
        problems.append(f"[9] 停止デーモン: {', '.join(dead)}")

    return problems


def render_docs(section: str | None = None) -> str:
    """関連ドキュメントへのリンク."""
    lines = ["=== 関連ドキュメント ==="]
    sections = [section] if section else SECTION_DOCS.keys()
    for sec in sections:
        docs = SECTION_DOCS.get(sec, [])
        if not docs:
            continue
        lines.append(f"\n[{sec}]")
        for d in docs:
            lines.append(f"  - {d}")
    return "\n".join(lines)


def render_section(data: dict, section_key: str, full: bool = False) -> str:
    """特定セクションのみをレンダリング."""
    # 全体レンダリングから該当セクションだけ抽出
    full_render = render_summary(data, full=full)
    lines = full_render.splitlines()
    result = []
    in_section = False
    section_num = section_key.split("_")[0]
    target_marker = f"[{section_num}]"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(target_marker):
            in_section = True
            result.append(line)
            continue
        if in_section:
            # 次のセクションに入ったら終了
            if stripped.startswith("[") and stripped[1:2].isdigit():
                break
            result.append(line)
    return "\n".join(result) if result else f"セクション {section_key} が見つかりません"


def main():
    full = "--full" in sys.argv
    json_out = "--json" in sys.argv
    problems_only = "--problems" in sys.argv
    docs_only = "--docs" in sys.argv
    section_arg = None
    if "--section" in sys.argv:
        idx = sys.argv.index("--section")
        if idx + 1 < len(sys.argv):
            section_arg = sys.argv[idx + 1]

    # --docs 単独呼び出し
    if docs_only and not section_arg:
        print(render_docs())
        return

    data = collect_all()

    if json_out:
        # 特定セクションのJSONのみ
        if section_arg:
            sec = resolve_section(section_arg)
            if sec and sec in data["sections"]:
                print(json.dumps({sec: data["sections"][sec]}, ensure_ascii=False, indent=2))
                return
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    # 問題のみ表示
    if problems_only:
        problems = extract_problems(data)
        if not problems:
            print("✓ 問題なし (全9セクション正常)")
        else:
            print(f"!! 検出された問題 ({len(problems)}件) !!")
            for p in problems:
                print(f"  {p}")
        return

    # 特定セクション
    if section_arg:
        sec = resolve_section(section_arg)
        if not sec:
            print(f"セクション解決失敗: '{section_arg}'")
            print(f"有効なキーワード: {list(SECTION_KEYWORDS.keys())}")
            sys.exit(1)
        print(render_section(data, sec, full=full))
        if docs_only:
            print()
            print(render_docs(sec))
        return

    # 全体表示
    print(render_summary(data, full=full))

    # 問題サマリー (常時表示)
    problems = extract_problems(data)
    if problems:
        print(f"\n!! 検出された問題 ({len(problems)}件) !!")
        for p in problems:
            print(f"  {p}")
        print("\n詳細: python scripts/helix_overview.py --problems")


if __name__ == "__main__":
    main()
