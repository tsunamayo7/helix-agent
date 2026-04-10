"""System Auditor — 全体的な定期監査+改善提案.

トークン消費: Stage 1-4はゼロ、Stage 5のみgemma4(無料)
実行: タスクスケジューラで6時間ごと or 手動

6つの監査レイヤー:
  1. セキュリティ監査 (Pure Python)
  2. 構造整合性監査 (Pure Python)
  3. データ品質監査 (Pure Python + HTTP)
  4. パフォーマンス監査 (Pure Python)
  5. 改善提案 (gemma4, $0) — 最新情報+トレンド活用
  6. レポート生成+通知

使い方:
    python scripts/system_auditor.py              # 全監査
    python scripts/system_auditor.py --quick       # 重要項目のみ(2分)
    python scripts/system_auditor.py report        # 最新レポート表示
    python scripts/system_auditor.py improvements  # 改善提案のみ
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
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
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
MEMORY_DIR = CLAUDE_DIR / "projects" / "C--Development" / "memory"
HELIX_AGENT_DIR = Path("C:/Development/tools/helix-agent")
SCRIPTS_DIR = HELIX_AGENT_DIR / "scripts"
SRC_DIR = HELIX_AGENT_DIR / "src"

REPORT_DIR = HELIX_DIR / "audit_reports"
LATEST_REPORT = REPORT_DIR / "latest.json"
HISTORY_DIR = REPORT_DIR / "history"

WEBHOOK_SCRIPT = HOOKS_DIR / "discord_webhook_fallback.py"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

OLLAMA_URL = "http://localhost:11434"

# 重要度に基づく影響度スコア
CRITICAL_COMPONENTS = {
    "discord_webhook": {"path": WEBHOOK_SCRIPT, "impact": 10, "desc": "全通知基盤"},
    "qdrant": {"url": "http://localhost:6333/collections", "impact": 9, "desc": "記憶DB"},
    "ollama": {"url": f"{OLLAMA_URL}/api/tags", "impact": 8, "desc": "ローカルLLM"},
    "supervisor": {"heartbeat": "supervisor", "impact": 8, "desc": "デーモン監視"},
    "smart_approval": {"path": HOOKS_DIR / "smart_approval.py", "impact": 7, "desc": "自動承認"},
    "pretool_security": {"path": HOOKS_DIR / "pretool_security.py", "impact": 7, "desc": "セキュリティ"},
    "health_server": {"url": "http://localhost:8800/health", "impact": 6, "desc": "ヘルスチェック"},
    "session_checkpoint": {"path": HOOKS_DIR / "session_checkpoint.py", "impact": 6, "desc": "チェックポイント"},
    "dashboard": {"url": "http://localhost:8801/api/status", "impact": 5, "desc": "ダッシュボード"},
}


# ---------------------------------------------------------------------------
# Layer 1: セキュリティ監査
# ---------------------------------------------------------------------------


def audit_security() -> list[dict]:
    """セキュリティチェック."""
    findings = []

    # 1. Hook構文チェック
    for hook_file in HOOKS_DIR.glob("*.py"):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(hook_file)],
                capture_output=True, text=True, timeout=10,
                creationflags=NO_WINDOW,
            )
            if result.returncode != 0:
                findings.append({
                    "severity": "HIGH",
                    "category": "security",
                    "component": hook_file.name,
                    "message": f"フック構文エラー: {result.stderr[:100]}",
                    "fix": f"python -m py_compile {hook_file} で確認",
                })
        except Exception:
            pass

    # 2. settings.json permissions チェック
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        perms = settings.get("permissions", {})

        # deny リストの重要項目確認
        deny = perms.get("deny", [])
        essential_denies = [".ssh", "rm -rf", "format", "diskpart", "--force"]
        for pattern in essential_denies:
            if not any(pattern in d for d in deny):
                findings.append({
                    "severity": "HIGH",
                    "category": "security",
                    "component": "settings.json",
                    "message": f"denyリストに '{pattern}' パターンがありません",
                    "fix": "settings.json の deny リストに追加",
                })

        # model形式チェック
        model = settings.get("model", "")
        if not re.match(r'claude-[\w-]+', model):
            findings.append({
                "severity": "MEDIUM",
                "category": "security",
                "component": "settings.json",
                "message": f"model形式が不正: {model}",
            })

    except (json.JSONDecodeError, OSError) as e:
        findings.append({
            "severity": "CRITICAL",
            "category": "security",
            "component": "settings.json",
            "message": f"読み込み不可: {e}",
        })

    # 3. 承認ルールの異常検出
    rules_file = HELIX_DIR / "approval_rules" / "rules.json"
    if rules_file.exists():
        try:
            rules = json.loads(rules_file.read_text(encoding="utf-8"))
            rule_list = rules.get("rules", [])
            # 過度に広いルール
            for rule in rule_list:
                pattern = rule.get("pattern", "")
                if pattern in (".*", "Bash:.*", ""):
                    findings.append({
                        "severity": "HIGH",
                        "category": "security",
                        "component": "approval_rules",
                        "message": f"過度に広い承認ルール: {pattern}",
                        "fix": "ルールを具体化するか削除",
                    })
        except (json.JSONDecodeError, OSError):
            pass

    # 4. 公開ポートチェック
    for name, info in CRITICAL_COMPONENTS.items():
        url = info.get("url", "")
        if url and "localhost" not in url and "127.0.0.1" not in url:
            findings.append({
                "severity": "MEDIUM",
                "category": "security",
                "component": name,
                "message": f"外部バインドの可能性: {url}",
            })

    return findings


# ---------------------------------------------------------------------------
# Layer 2: 構造整合性監査
# ---------------------------------------------------------------------------


def audit_structure() -> list[dict]:
    findings = []

    # 1. Hook → settings.json 整合性
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        hooks = settings.get("hooks", {})
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                for hook in group.get("hooks", []):
                    cmd = hook.get("command", "")
                    parts = cmd.split()
                    if len(parts) >= 2:
                        script = Path(parts[-1])
                        if not script.exists():
                            findings.append({
                                "severity": "HIGH",
                                "category": "structure",
                                "component": f"hooks.{event}",
                                "message": f"フックスクリプト不在: {script}",
                                "fix": f"ファイルを復元するか hooks設定を修正",
                            })

        # MCP サーバー整合性
        for name, config in settings.get("mcpServers", {}).items():
            if config.get("enabled") is False:
                continue
            args = config.get("args", [])
            for arg in args:
                if arg.endswith(".py") or arg.endswith(".js"):
                    p = Path(arg)
                    if not p.exists():
                        findings.append({
                            "severity": "MEDIUM",
                            "category": "structure",
                            "component": f"mcp.{name}",
                            "message": f"MCPスクリプト不在: {arg}",
                        })
    except (json.JSONDecodeError, OSError):
        findings.append({
            "severity": "CRITICAL",
            "category": "structure",
            "component": "settings.json",
            "message": "settings.json 読み込み不可",
        })

    # 2. scripts/ のimportチェック（構文のみ）
    for py_file in SCRIPTS_DIR.glob("*.py"):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(py_file)],
                capture_output=True, text=True, timeout=10,
                creationflags=NO_WINDOW,
            )
            if result.returncode != 0:
                findings.append({
                    "severity": "MEDIUM",
                    "category": "structure",
                    "component": py_file.name,
                    "message": f"構文エラー: {result.stderr[:80]}",
                })
        except Exception:
            pass

    # 3. MEMORY.md とファイルの整合性
    memory_md = MEMORY_DIR / "MEMORY.md"
    if memory_md.exists():
        content = memory_md.read_text(encoding="utf-8")
        # リンク先ファイルの存在確認
        links = re.findall(r'\[.*?\]\(([^)]+\.md)\)', content)
        for link in links:
            if link.startswith("http"):
                continue
            target = MEMORY_DIR / link
            if not target.exists():
                findings.append({
                    "severity": "LOW",
                    "category": "structure",
                    "component": "MEMORY.md",
                    "message": f"リンク先不在: {link}",
                })

    # 4. start_claude.bat 参照ファイルの存在
    bat_path = Path("C:/Development/start/manual/start_claude.bat")
    if bat_path.exists():
        bat_content = bat_path.read_text(encoding="utf-8")
        # スクリプト参照の抽出
        refs = re.findall(r'python\s+([^\s"]+\.py)', bat_content)
        for ref in refs:
            ref_path = Path(ref)
            if not ref_path.exists() and not (HELIX_AGENT_DIR / ref).exists():
                findings.append({
                    "severity": "MEDIUM",
                    "category": "structure",
                    "component": "start_claude.bat",
                    "message": f"参照スクリプト不在: {ref}",
                })

    return findings


# ---------------------------------------------------------------------------
# Layer 3: データ品質監査
# ---------------------------------------------------------------------------


def audit_data() -> list[dict]:
    findings = []

    # 1. サービス健全性
    services = {
        "Qdrant": "http://localhost:6333/collections",
        "Ollama": f"{OLLAMA_URL}/api/tags",
        "HealthServer": "http://localhost:8800/health",
        "Dashboard": "http://localhost:8801/api/status",
    }
    for name, url in services.items():
        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception:
            findings.append({
                "severity": "HIGH" if name in ("Qdrant", "Ollama") else "MEDIUM",
                "category": "data",
                "component": name,
                "message": f"{name}に接続不可 ({url})",
            })

    # 2. デーモン心拍チェック
    hb_dir = HELIX_DIR / "heartbeats"
    if hb_dir.exists():
        now = datetime.now(timezone.utc)
        for hb_file in hb_dir.glob("*.json"):
            try:
                hb = json.loads(hb_file.read_text(encoding="utf-8"))
                ts = hb.get("timestamp", "")
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_min = (now - dt).total_seconds() / 60
                if age_min > 30:
                    findings.append({
                        "severity": "MEDIUM",
                        "category": "data",
                        "component": f"daemon.{hb_file.stem}",
                        "message": f"{hb_file.stem}が{int(age_min)}分間応答なし",
                        "fix": "タスクスケジューラで該当タスクを確認",
                    })
            except (json.JSONDecodeError, ValueError, OSError):
                pass

    # 3. バックアップ鮮度
    backup_dirs = sorted(MEMORY_DIR.glob("_backup_*"), reverse=True)
    if backup_dirs:
        latest_backup = backup_dirs[0]
        age_hours = (time.time() - latest_backup.stat().st_mtime) / 3600
        if age_hours > 48:
            findings.append({
                "severity": "MEDIUM",
                "category": "data",
                "component": "backup",
                "message": f"最新バックアップが{int(age_hours)}時間前",
                "fix": "python scripts/backup_to_nas.py を実行",
            })
    else:
        findings.append({
            "severity": "HIGH",
            "category": "data",
            "component": "backup",
            "message": "バックアップが存在しません",
        })

    # 4. 監査ログサイズ
    audit_log = HELIX_DIR / "approval_rules" / "audit.jsonl"
    if audit_log.exists() and audit_log.stat().st_size > 5 * 1024 * 1024:
        findings.append({
            "severity": "LOW",
            "category": "data",
            "component": "audit_log",
            "message": f"監査ログが{audit_log.stat().st_size / 1024 / 1024:.1f}MBに肥大化",
            "fix": "古いエントリを削除",
        })

    # 5. memory/ファイル品質
    for md_file in MEMORY_DIR.glob("*.md"):
        if md_file.name.startswith("_") or md_file.name == "MEMORY.md":
            continue
        stat = md_file.stat()
        if stat.st_size == 0:
            findings.append({
                "severity": "LOW",
                "category": "data",
                "component": f"memory/{md_file.name}",
                "message": "空ファイル",
                "fix": "削除または内容を追加",
            })

    # 6. スケジュールタスク最終実行閾値チェック
    findings.extend(_check_task_freshness())

    # 7. Qdrantコレクションポイント数トレンド
    findings.extend(_check_qdrant_trends())

    # 8. マニフェストと実態のサービス比較
    findings.extend(_check_manifest_drift())

    return findings


# ---------------------------------------------------------------------------
# Layer 3 拡張: 環境保守ヘルパー
# ---------------------------------------------------------------------------


def _check_task_freshness() -> list[dict]:
    """スケジュールタスクの最終実行が閾値内か確認.

    タスクごとの想定間隔の3倍以内に実行されていない場合は異常。
    ContradictionCheck/QdrantDedumなど長期間実行されていないタスクを検出。
    """
    findings = []
    # name: max_age_hours (許容最終実行からの経過時間)
    expected = {
        "Helix-Supervisor": 0.25,          # 3分 → 15分以内
        "Helix-Watchdog": 1,
        "Helix-AssistantDaemon": 1,
        "Helix-Backup": 48,                # 日次なので2日許容
        "Helix-IntegrityCheck": 48,
        "Helix-MemoryHealth": 48,
        "Helix-ContradictionCheck": 24 * 9,  # 週次なので9日許容
        "Helix-QdrantDedup": 24 * 9,
        "Helix-DeptFeed": 2,
        "Helix-Failover": 1,
        "Helix-Escalation": 1,
        "Helix-SystemAudit": 15,
    }
    for task_name, max_age_h in expected.items():
        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"$i = Get-ScheduledTaskInfo -TaskName '{task_name}' -EA SilentlyContinue; "
                    f"if ($i) {{ $i.LastRunTime.ToString('o') }} else {{ 'MISSING' }}"
                ],
                capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
            )
            output = result.stdout.strip()
            if output == "MISSING":
                findings.append({
                    "severity": "HIGH",
                    "category": "data",
                    "component": f"task.{task_name}",
                    "message": "タスクがスケジューラに登録されていません",
                    "fix": "schtasks /create で再登録",
                })
                continue
            if output.startswith("1999") or not output:
                findings.append({
                    "severity": "HIGH",
                    "category": "data",
                    "component": f"task.{task_name}",
                    "message": "一度も実行されていません (未実行状態)",
                    "fix": "Trigger設定を確認、手動実行でテスト",
                })
                continue
            # ISO形式の日時をパース
            try:
                last_run = datetime.fromisoformat(output)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600
                if age_h > max_age_h:
                    findings.append({
                        "severity": "MEDIUM" if age_h < max_age_h * 2 else "HIGH",
                        "category": "data",
                        "component": f"task.{task_name}",
                        "message": f"最終実行から{int(age_h)}時間経過(閾値{max_age_h}h)",
                        "fix": "schtasks /run で手動実行して確認",
                    })
            except ValueError:
                pass
        except Exception:
            pass
    return findings


def _check_qdrant_trends() -> list[dict]:
    """Qdrantコレクションのポイント数トレンドを記録+急増/急減を検出."""
    findings = []
    trend_file = HELIX_DIR / "qdrant_trends.jsonl"
    trend_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        resp = urllib.request.urlopen("http://localhost:6333/collections", timeout=5)
        data = json.loads(resp.read().decode())
        collections = [c["name"] for c in data.get("result", {}).get("collections", [])]
    except Exception as e:
        findings.append({
            "severity": "MEDIUM",
            "category": "data",
            "component": "qdrant",
            "message": f"コレクション一覧取得失敗: {e}",
        })
        return findings

    current_counts = {}
    for col in collections:
        try:
            resp = urllib.request.urlopen(
                f"http://localhost:6333/collections/{col}", timeout=5
            )
            info = json.loads(resp.read().decode())
            current_counts[col] = info.get("result", {}).get("points_count", 0)
        except Exception:
            pass

    # 前回値をロード
    previous_counts = {}
    if trend_file.exists():
        try:
            lines = trend_file.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                last_entry = json.loads(lines[-1])
                previous_counts = last_entry.get("counts", {})
        except Exception:
            pass

    # 急減/急増検出 (50%超)
    for col, current in current_counts.items():
        prev = previous_counts.get(col)
        if prev is None or prev == 0:
            continue
        delta_pct = (current - prev) / prev * 100
        if delta_pct < -20:
            findings.append({
                "severity": "HIGH",
                "category": "data",
                "component": f"qdrant.{col}",
                "message": f"ポイント数が{prev}→{current} ({delta_pct:+.1f}%)急減",
                "fix": "データ削除が意図的か確認、バックアップから復元",
            })
        elif delta_pct > 1000:
            findings.append({
                "severity": "LOW",
                "category": "data",
                "component": f"qdrant.{col}",
                "message": f"ポイント数が{prev}→{current} ({delta_pct:+.1f}%)急増",
            })

    # 今回値を記録
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "counts": current_counts,
        }
        with trend_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # 過去100件のみ保持
        lines = trend_file.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) > 100:
            trend_file.write_text("\n".join(lines[-100:]) + "\n", encoding="utf-8")
    except Exception:
        pass

    return findings


def _check_manifest_drift() -> list[dict]:
    """environment_manifest.yamlと実態のサービス/ファイル差分を検出."""
    findings = []
    manifest_path = HELIX_AGENT_DIR / "config" / "environment_manifest.yaml"
    if not manifest_path.exists():
        return findings  # マニフェスト無しならスキップ

    try:
        import yaml
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        findings.append({
            "severity": "LOW",
            "category": "structure",
            "component": "manifest",
            "message": f"マニフェスト読み込み失敗: {e}",
        })
        return findings

    # サービスURL確認 (criticality high以上のみ)
    for name, cfg in (manifest.get("services") or {}).items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("criticality") not in ("critical", "high"):
            continue
        url = cfg.get("url")
        if not url:
            continue
        endpoint = cfg.get("health_endpoint", "")
        try:
            urllib.request.urlopen(url + endpoint, timeout=3)
        except Exception:
            findings.append({
                "severity": "HIGH" if cfg.get("criticality") == "critical" else "MEDIUM",
                "category": "data",
                "component": f"service.{name}",
                "message": f"マニフェスト定義のサービスが応答なし: {url}{endpoint}",
                "fix": f"{cfg.get('script', cfg.get('start_command', '?'))} を起動",
            })

    # 重要パスの存在確認
    for key, path in (manifest.get("paths") or {}).items():
        if not isinstance(path, str):
            continue
        if not Path(path).exists():
            findings.append({
                "severity": "HIGH",
                "category": "structure",
                "component": f"path.{key}",
                "message": f"マニフェスト定義のパスが存在しない: {path}",
            })

    # Python環境の存在確認
    for env_name, cfg in (manifest.get("python_environments") or {}).items():
        if not isinstance(cfg, dict):
            continue
        py_path = cfg.get("path")
        if py_path and not Path(py_path).exists():
            findings.append({
                "severity": "HIGH",
                "category": "structure",
                "component": f"python.{env_name}",
                "message": f"Python環境が見つからない: {py_path}",
            })

    return findings


# ---------------------------------------------------------------------------
# Layer 4: パフォーマンス監査
# ---------------------------------------------------------------------------


def audit_performance() -> list[dict]:
    findings = []

    # 1. ディスク使用量
    try:
        import shutil
        for drive in ["C:", "D:", "F:"]:
            try:
                usage = shutil.disk_usage(drive + "\\")
                pct = usage.used / usage.total * 100
                if pct > 90:
                    findings.append({
                        "severity": "HIGH",
                        "category": "performance",
                        "component": f"disk.{drive}",
                        "message": f"{drive} 使用率 {pct:.1f}%（残り{usage.free / 1024**3:.1f}GB）",
                    })
                elif pct > 80:
                    findings.append({
                        "severity": "MEDIUM",
                        "category": "performance",
                        "component": f"disk.{drive}",
                        "message": f"{drive} 使用率 {pct:.1f}%",
                    })
            except (OSError, FileNotFoundError):
                continue
    except ImportError:
        pass

    # 2. CLAUDE.md サイズ（トークン消費）
    claude_md = CLAUDE_DIR / "CLAUDE.md"
    if claude_md.exists():
        size_kb = claude_md.stat().st_size / 1024
        if size_kb > 30:
            findings.append({
                "severity": "MEDIUM",
                "category": "performance",
                "component": "CLAUDE.md",
                "message": f"CLAUDE.mdが{size_kb:.0f}KB（~{int(size_kb * 25)}トークン/セッション消費）",
                "fix": "圧縮を検討",
            })

    # 3. memory/ファイル数
    md_count = len(list(MEMORY_DIR.glob("*.md")))
    if md_count > 60:
        findings.append({
            "severity": "LOW",
            "category": "performance",
            "component": "memory/",
            "message": f"記憶ファイル{md_count}件（MEMORY.mdの行数超過リスク）",
            "fix": "archive/ への移動を検討",
        })

    # 4. Qdrantコレクションサイズ
    try:
        resp = urllib.request.urlopen("http://localhost:6333/collections", timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        for col in data.get("result", {}).get("collections", []):
            name = col.get("name", "")
            try:
                col_resp = urllib.request.urlopen(f"http://localhost:6333/collections/{name}", timeout=5)
                col_data = json.loads(col_resp.read().decode("utf-8"))
                points = col_data.get("result", {}).get("points_count", 0)
                if points > 5000:
                    findings.append({
                        "severity": "LOW",
                        "category": "performance",
                        "component": f"qdrant.{name}",
                        "message": f"{name}: {points}ポイント（重複チェック推奨）",
                    })
            except Exception:
                pass
    except Exception:
        pass

    return findings


# ---------------------------------------------------------------------------
# Layer 5: 改善提案 (gemma4)
# ---------------------------------------------------------------------------


def generate_improvements(all_findings: list[dict]) -> list[dict]:
    """gemma4で改善提案を生成. トークン消費: $0."""
    suggestions = []

    # 5a. ルールベース改善提案（gemma4不要）
    suggestions.extend(_rule_based_improvements())

    # 5b. x-feed-collector の最新データから改善機会を抽出
    suggestions.extend(_feed_based_improvements())

    # 5c. gemma4による総合分析（Ollamaが動いている場合のみ）
    gemma4_suggestions = _gemma4_analysis(all_findings, suggestions)
    if gemma4_suggestions:
        suggestions.extend(gemma4_suggestions)

    return suggestions


def _rule_based_improvements() -> list[dict]:
    """ルールベースの改善提案."""
    suggestions = []

    # バージョン更新チェック
    preflight_state = HELIX_DIR / "preflight" / "state.json"
    if preflight_state.exists():
        try:
            state = json.loads(preflight_state.read_text(encoding="utf-8"))
            history = state.get("history", [])
            if history:
                latest = history[-1]
                suggestions.append({
                    "category": "update",
                    "priority": "HIGH",
                    "message": f"Claude Code更新あり: {latest.get('from')} → {latest.get('to')}",
                    "action": "CLAUDE.md/settings.jsonの互換性確認",
                })
        except (json.JSONDecodeError, OSError):
            pass

    # 承認ルールの成長度チェック
    rules_file = HELIX_DIR / "approval_rules" / "rules.json"
    if rules_file.exists():
        try:
            rules = json.loads(rules_file.read_text(encoding="utf-8"))
            rule_count = len(rules.get("rules", []))
            if rule_count < 10:
                suggestions.append({
                    "category": "efficiency",
                    "priority": "MEDIUM",
                    "message": f"承認ルール{rule_count}件（学習初期段階）",
                    "action": "通常使用で自動学習が進む。2-3セッション後に大幅改善",
                })
        except (json.JSONDecodeError, OSError):
            pass

    # Opus レビュー待ちチェック
    pending = HELIX_DIR / "approval_rules" / "pending_review.json"
    if pending.exists():
        try:
            items = json.loads(pending.read_text(encoding="utf-8"))
            if len(items) > 5:
                suggestions.append({
                    "category": "maintenance",
                    "priority": "MEDIUM",
                    "message": f"Opusレビュー待ち{len(items)}件",
                    "action": "セッション開始時にレビューを実行",
                })
        except (json.JSONDecodeError, OSError):
            pass

    return suggestions


def _feed_based_improvements() -> list[dict]:
    """x-feed-collector/Qdrantの最新データから改善機会を検出."""
    suggestions = []

    try:
        # Qdrant mem0_shared から最近のAI関連ニュースを検索
        payload = json.dumps({
            "vector": [0] * 4096,  # ダミー（scroll APIを使う）
            "limit": 20,
            "filter": {
                "must": [
                    {"key": "source", "match": {"value": "x-feed-collector"}},
                ]
            },
            "with_payload": True,
        }).encode("utf-8")

        # scroll APIで最新データを取得
        req = urllib.request.Request(
            "http://localhost:6333/collections/mem0_shared/points/scroll",
            data=json.dumps({
                "limit": 30,
                "filter": {
                    "must": [
                        {"key": "source", "match": {"value": "x-feed-collector"}}
                    ]
                },
                "with_payload": True,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))

        points = data.get("result", {}).get("points", [])
        # Claude/MCP/Ollama関連のアップデートをフィルタ
        keywords = ["claude", "mcp", "ollama", "gemma", "qwen", "codex", "update", "release", "security", "vulnerability"]
        for point in points:
            payload = point.get("payload", {})
            content = str(payload.get("memory", payload.get("content", ""))).lower()
            for kw in keywords:
                if kw in content:
                    text = payload.get("memory", payload.get("content", ""))[:120]
                    suggestions.append({
                        "category": "intelligence",
                        "priority": "LOW",
                        "message": f"関連情報: {text}",
                        "action": "詳細調査を検討",
                        "source": "x-feed-collector",
                    })
                    break

        # 重複除去、最新5件のみ
        seen = set()
        unique = []
        for s in suggestions:
            key = s["message"][:50]
            if key not in seen:
                seen.add(key)
                unique.append(s)
        suggestions = unique[:5]

    except Exception:
        pass

    return suggestions


def _gemma4_analysis(findings: list[dict], existing_suggestions: list[dict]) -> list[dict]:
    """gemma4による総合分析."""
    try:
        # 監査結果の要約
        high_count = len([f for f in findings if f.get("severity") in ("CRITICAL", "HIGH")])
        med_count = len([f for f in findings if f.get("severity") == "MEDIUM"])
        categories = set(f.get("category", "") for f in findings)

        summary = f"""システム監査結果:
- 重大問題: {high_count}件
- 中程度問題: {med_count}件
- カテゴリ: {', '.join(categories)}
- 既存提案: {len(existing_suggestions)}件

主な問題:
{chr(10).join(f'- [{f["severity"]}] {f["message"]}' for f in findings[:10])}

このWindows開発PCの管理者として、以下を3件以内で提案:
1. 見落としがちなリスク
2. パフォーマンス改善
3. 自動化の改善機会

JSON配列で回答: [{{"category":"...", "priority":"HIGH|MEDIUM|LOW", "message":"...", "action":"..."}}]"""

        data = json.dumps({
            "model": "gemma4:31b",
            "prompt": summary,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 300},
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read().decode("utf-8"))
        response_text = result.get("response", "")

        # JSONパース
        json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
        if json_match:
            items = json.loads(json_match.group())
            return [item for item in items if isinstance(item, dict)][:3]

    except Exception:
        pass

    return []


# ---------------------------------------------------------------------------
# Layer 6: レポート生成
# ---------------------------------------------------------------------------


def generate_report(findings: list[dict], improvements: list[dict]) -> dict:
    """監査レポートを生成."""
    now = datetime.now(timezone.utc)
    jst = now + timedelta(hours=9)

    report = {
        "timestamp": now.isoformat(),
        "timestamp_jst": jst.strftime("%Y-%m-%d %H:%M JST"),
        "summary": {
            "total_findings": len(findings),
            "critical": len([f for f in findings if f.get("severity") == "CRITICAL"]),
            "high": len([f for f in findings if f.get("severity") == "HIGH"]),
            "medium": len([f for f in findings if f.get("severity") == "MEDIUM"]),
            "low": len([f for f in findings if f.get("severity") == "LOW"]),
            "improvements": len(improvements),
        },
        "findings": findings,
        "improvements": improvements,
    }

    # 保存
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 履歴保存
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    history_file = HISTORY_DIR / f"audit_{jst.strftime('%Y%m%d_%H%M')}.json"
    history_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 古い履歴を削除（30日分保持）
    cutoff = time.time() - 30 * 86400
    for old in HISTORY_DIR.glob("audit_*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)

    return report


def send_report_notification(report: dict) -> None:
    """重要な問題がある場合にDiscord通知."""
    summary = report["summary"]
    if summary["critical"] > 0 or summary["high"] > 0:
        msg_parts = [f"System Audit: {summary['critical']}件CRITICAL, {summary['high']}件HIGH"]
        for f in report["findings"][:5]:
            if f.get("severity") in ("CRITICAL", "HIGH"):
                msg_parts.append(f"- [{f['severity']}] {f['component']}: {f['message'][:60]}")

        if report.get("improvements"):
            msg_parts.append(f"\n改善提案: {len(report['improvements'])}件")

        if WEBHOOK_SCRIPT.exists():
            try:
                subprocess.run(
                    [sys.executable, str(WEBHOOK_SCRIPT), "\n".join(msg_parts)],
                    capture_output=True, text=True, timeout=30,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def run_full_audit(quick: bool = False) -> dict:
    """全監査実行."""
    print("=== System Auditor ===")
    all_findings = []

    # Layer 1
    print("\n[1/5] セキュリティ監査...")
    sec = audit_security()
    all_findings.extend(sec)
    print(f"  {len(sec)}件検出")

    # Layer 2
    print("[2/5] 構造整合性監査...")
    struct = audit_structure()
    all_findings.extend(struct)
    print(f"  {len(struct)}件検出")

    # Layer 3
    print("[3/5] データ品質監査...")
    data = audit_data()
    all_findings.extend(data)
    print(f"  {len(data)}件検出")

    # Layer 4
    print("[4/5] パフォーマンス監査...")
    perf = audit_performance()
    all_findings.extend(perf)
    print(f"  {len(perf)}件検出")

    # Layer 5
    improvements = []
    if not quick:
        print("[5/5] 改善提案生成...")
        improvements = generate_improvements(all_findings)
        print(f"  {len(improvements)}件提案")
    else:
        print("[5/5] スキップ (--quick)")

    # Layer 6
    print("\n[レポート生成]")
    report = generate_report(all_findings, improvements)
    send_report_notification(report)

    # サマリー表示
    s = report["summary"]
    print(f"\n{'='*40}")
    print(f"  CRITICAL: {s['critical']}  HIGH: {s['high']}  MEDIUM: {s['medium']}  LOW: {s['low']}")
    print(f"  改善提案: {s['improvements']}件")
    print(f"  レポート: {LATEST_REPORT}")
    print(f"{'='*40}")

    if all_findings:
        print("\n[問題一覧]")
        for f in sorted(all_findings, key=lambda x: {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(x.get("severity", ""), 9)):
            sev = f["severity"]
            icon = {"CRITICAL": "X", "HIGH": "!", "MEDIUM": "?", "LOW": "-"}.get(sev, " ")
            print(f"  [{icon}] {sev:8s} | {f.get('component', '?'):25s} | {f['message'][:60]}")

    if improvements:
        print("\n[改善提案]")
        for imp in improvements:
            print(f"  [{imp.get('priority', '?')}] {imp.get('category', '?')}: {imp.get('message', '')[:70]}")
            if imp.get("action"):
                print(f"       -> {imp['action'][:70]}")

    return report


def show_report():
    """最新レポートを表示."""
    if not LATEST_REPORT.exists():
        print("レポートがありません。python scripts/system_auditor.py を実行してください。")
        return
    report = json.loads(LATEST_REPORT.read_text(encoding="utf-8"))
    print(f"=== 最新監査レポート ({report.get('timestamp_jst', '?')}) ===")
    s = report["summary"]
    print(f"  CRITICAL: {s['critical']}  HIGH: {s['high']}  MEDIUM: {s['medium']}  LOW: {s['low']}")
    for f in report.get("findings", []):
        print(f"  [{f['severity']}] {f.get('component', '?')}: {f['message'][:80]}")
    if report.get("improvements"):
        print(f"\n[改善提案]")
        for imp in report["improvements"]:
            print(f"  [{imp.get('priority', '?')}] {imp.get('message', '')[:80]}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "report" in args:
        show_report()
    elif "improvements" in args:
        improvements = generate_improvements([])
        for imp in improvements:
            print(f"[{imp.get('priority', '?')}] {imp.get('category', '?')}: {imp.get('message', '')}")
    elif "--quick" in args:
        run_full_audit(quick=True)
    else:
        run_full_audit()
