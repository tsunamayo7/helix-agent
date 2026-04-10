"""Backup to NAS — 3層バックアップシステム.

L0: ローカルスナップショット (memory/_backup_YYYYMMDD/)
L1: NAS日次バックアップ (age暗号化)
L2: NAS週次フルバックアップ (age暗号化)

NAS未設定時はL0のみ動作。config.jsonでNASパスを設定後にL1/L2有効化。

使い方:
    python scripts/backup_to_nas.py              # L0 + L1(設定時)
    python scripts/backup_to_nas.py --local-only  # L0のみ
    python scripts/backup_to_nas.py --weekly       # L0 + L2フルバックアップ
    python scripts/backup_to_nas.py --restore      # 復元ガイド表示
    python scripts/backup_to_nas.py status         # バックアップ状態表示
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Windows cp932対策
if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

MEMORY_DIR = Path.home() / ".claude" / "projects" / "C--Development" / "memory"
CMEM_DB = Path.home() / ".claude-mem" / "claude-mem.db"
SETTINGS_JSON = Path.home() / ".claude" / "settings.json"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
LIGHTRAG_STORAGE = Path("C:/Development/tools/lightrag-server/rag_storage")
QDRANT_URL = "http://localhost:6333"
COLLECTION = "mem0_shared"

AGE_KEY_FILE = Path.home() / ".age" / "key.txt"
CONFIG_DIR = Path.home() / ".helix-agent" / "backup"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"

# デフォルト設定
DEFAULT_CONFIG = {
    "local": {
        "enabled": True,
        "path": str(MEMORY_DIR / "_backup_{date}"),
        "max_age_days": 7,
    },
    "nas": {
        "enabled": False,
        "path": "",                    # \\NAS_IP\share\backup\claude-memory
        "daily_time": "03:00",
        "daily_max_age_days": 30,
        "weekly_day": "sunday",
        "weekly_max_age_days": 90,
    },
    "encryption": {
        "enabled": True,
        "age_public_key": "",          # age1...で始まる公開鍵
    },
    "exclude_patterns": [
        ".env*", "credentials*", "*.pem", "*.key",
        "__pycache__", "*.pyc", ".git",
    ],
}

# バックアップ対象
BACKUP_TARGETS = [
    {"name": "memory", "path": MEMORY_DIR, "method": "copy_dir"},
    {"name": "cmem", "path": CMEM_DB, "method": "sqlite_backup"},
    {"name": "settings", "path": SETTINGS_JSON, "method": "copy_file"},
    {"name": "claude_md", "path": CLAUDE_MD, "method": "copy_file"},
    {"name": "lightrag", "path": LIGHTRAG_STORAGE, "method": "copy_dir"},
]


def load_config() -> dict:
    """バックアップ設定を読み込み."""
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            user_config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            # deep merge
            for key in user_config:
                if isinstance(config.get(key), dict) and isinstance(user_config[key], dict):
                    config[key].update(user_config[key])
                else:
                    config[key] = user_config[key]
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config: dict) -> None:
    """バックアップ設定を保存."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_state() -> dict:
    """バックアップ状態を読み込み."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"backups": []}


def save_state(state: dict) -> None:
    """バックアップ状態を保存."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_notification(message: str) -> bool:
    """Discord通知."""
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
# バックアップ操作
# ---------------------------------------------------------------------------

def copy_directory(src: Path, dst: Path, exclude: list[str]) -> int:
    """ディレクトリをコピー（除外パターン適用）."""
    if not src.exists():
        return 0
    count = 0
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        # 除外チェック
        skip = False
        for pattern in exclude:
            if rel.match(pattern) or any(p.match(pattern) for p in rel.parents):
                skip = True
                break
        if skip:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(item), str(target))
        count += 1
    return count


def sqlite_backup(src: Path, dst: Path) -> bool:
    """SQLiteデータベースの安全なバックアップ (.backup API使用)."""
    if not src.exists():
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(src), timeout=10)
        backup_conn = sqlite3.connect(str(dst))
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()
        return True
    except Exception as e:
        print(f"  SQLite backup failed: {e}")
        # フォールバック: ファイルコピー
        try:
            shutil.copy2(str(src), str(dst))
            return True
        except Exception:
            return False


def qdrant_snapshot(dst_dir: Path) -> bool:
    """Qdrantコレクションのスナップショットを取得."""
    try:
        # スナップショット作成
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{COLLECTION}/snapshots",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode("utf-8"))
        snapshot_name = data.get("result", {}).get("name", "")

        if not snapshot_name:
            print("  Qdrant snapshot: スナップショット名取得失敗")
            return False

        # スナップショットダウンロード
        download_url = f"{QDRANT_URL}/collections/{COLLECTION}/snapshots/{snapshot_name}"
        dst_file = dst_dir / f"qdrant_{snapshot_name}"
        dst_dir.mkdir(parents=True, exist_ok=True)

        req = urllib.request.Request(download_url)
        resp = urllib.request.urlopen(req, timeout=60)
        dst_file.write_bytes(resp.read())

        # スナップショット削除（サーバー側のクリーンアップ）
        try:
            del_req = urllib.request.Request(
                f"{QDRANT_URL}/collections/{COLLECTION}/snapshots/{snapshot_name}",
                method="DELETE",
            )
            urllib.request.urlopen(del_req, timeout=10)
        except Exception:
            pass

        print(f"  Qdrant snapshot: {dst_file.name} ({dst_file.stat().st_size // 1024}KB)")
        return True
    except Exception as e:
        print(f"  Qdrant snapshot failed: {e}")
        return False


def generate_checksums(backup_dir: Path) -> Path:
    """SHA-256チェックサムファイルを生成."""
    checksum_file = backup_dir / "checksums.sha256"
    lines = []
    for item in sorted(backup_dir.rglob("*")):
        if item.is_dir() or item.name == "checksums.sha256":
            continue
        try:
            h = hashlib.sha256(item.read_bytes()).hexdigest()
            rel = item.relative_to(backup_dir)
            lines.append(f"{h}  {rel}")
        except OSError:
            continue
    checksum_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksum_file


def encrypt_with_age(target_dir: Path, public_key: str) -> Path | None:
    """age公開鍵でディレクトリをtar.age暗号化."""
    if not public_key or not shutil.which("age"):
        return None

    tar_file = target_dir.with_suffix(".tar")
    age_file = target_dir.with_suffix(".tar.age")

    try:
        # tar作成
        shutil.make_archive(str(target_dir), "tar", str(target_dir.parent), target_dir.name)

        # age暗号化
        no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            ["age", "-r", public_key, "-o", str(age_file), str(tar_file)],
            capture_output=True, text=True, timeout=300,
            creationflags=no_window,
        )
        if result.returncode == 0:
            # 元tarを削除
            tar_file.unlink(missing_ok=True)
            return age_file
        else:
            print(f"  age encryption failed: {result.stderr}")
            tar_file.unlink(missing_ok=True)
            return None
    except Exception as e:
        print(f"  age encryption error: {e}")
        tar_file.unlink(missing_ok=True)
        return None


def prune_old_backups(base_dir: Path, max_age_days: int, pattern: str = "_backup_*") -> int:
    """古いバックアップを削除."""
    if not base_dir.exists():
        return 0
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    for item in sorted(base_dir.glob(pattern)):
        try:
            if item.stat().st_mtime < cutoff:
                if item.is_dir():
                    shutil.rmtree(str(item))
                else:
                    item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# バックアップ実行
# ---------------------------------------------------------------------------

def run_local_backup(config: dict) -> dict:
    """L0: ローカルスナップショット."""
    timestamp = datetime.now().strftime("%Y%m%d")
    backup_dir = MEMORY_DIR / f"_backup_{timestamp}"
    exclude = config.get("exclude_patterns", [])

    print(f"\n=== L0: Local Backup → {backup_dir.name} ===")

    if backup_dir.exists():
        print(f"  既に存在: {backup_dir.name} (スキップ)")
        return {"status": "skipped", "path": str(backup_dir)}

    backup_dir.mkdir(parents=True, exist_ok=True)
    result = {"status": "ok", "path": str(backup_dir), "details": {}}

    # 1. memory/ ファイル (archive/除外)
    print("  [1/5] memory/ ファイル...")
    mem_dst = backup_dir / "memory"
    count = 0
    for f in MEMORY_DIR.glob("*.md"):
        (mem_dst).mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(f), str(mem_dst / f.name))
        count += 1
    # content/
    content_src = MEMORY_DIR / "content"
    if content_src.exists():
        count += copy_directory(content_src, mem_dst / "content", exclude)
    result["details"]["memory_files"] = count
    print(f"    {count} ファイル")

    # 2. Qdrant snapshot
    print("  [2/5] Qdrant snapshot...")
    qdrant_ok = qdrant_snapshot(backup_dir / "qdrant")
    result["details"]["qdrant"] = qdrant_ok

    # 3. $CMEM
    print("  [3/5] $CMEM DB...")
    cmem_ok = sqlite_backup(CMEM_DB, backup_dir / "cmem" / "claude-mem.db")
    result["details"]["cmem"] = cmem_ok
    if cmem_ok:
        size = (backup_dir / "cmem" / "claude-mem.db").stat().st_size
        print(f"    {size // 1024}KB")

    # 4. 設定ファイル
    print("  [4/5] 設定ファイル...")
    settings_dst = backup_dir / "settings"
    settings_dst.mkdir(parents=True, exist_ok=True)
    for target in BACKUP_TARGETS:
        if target["method"] == "copy_file" and target["path"].exists():
            shutil.copy2(str(target["path"]), str(settings_dst / target["path"].name))
    print("    OK")

    # 5. チェックサム
    print("  [5/5] チェックサム生成...")
    cs = generate_checksums(backup_dir)
    lines = cs.read_text(encoding="utf-8").strip().split("\n")
    result["details"]["checksum_count"] = len(lines)
    print(f"    {len(lines)} ファイル検証済み")

    # 古いバックアップ削除
    max_age = config.get("local", {}).get("max_age_days", 7)
    removed = prune_old_backups(MEMORY_DIR, max_age)
    if removed:
        print(f"  古いバックアップ {removed} 件削除 ({max_age}日超)")

    print(f"  L0完了: {backup_dir.name}")
    return result


def run_nas_backup(config: dict, weekly: bool = False) -> dict:
    """L1/L2: NASバックアップ (age暗号化)."""
    nas_config = config.get("nas", {})
    if not nas_config.get("enabled"):
        print("\n=== NAS Backup: 無効 (config.jsonでnas.enabled=trueに設定してください) ===")
        return {"status": "disabled"}

    nas_path = Path(nas_config.get("path", ""))
    if not nas_path.exists():
        print(f"\n=== NAS Backup: パス不到達 ({nas_path}) ===")
        return {"status": "unreachable", "path": str(nas_path)}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    subdir = "weekly" if weekly else "daily"
    backup_dir = nas_path / subdir / timestamp
    exclude = config.get("exclude_patterns", [])

    level = "L2 (Weekly Full)" if weekly else "L1 (Daily)"
    print(f"\n=== {level}: NAS Backup → {backup_dir} ===")

    backup_dir.mkdir(parents=True, exist_ok=True)
    result = {"status": "ok", "path": str(backup_dir), "details": {}}

    # memory/
    print("  [1/6] memory/...")
    mem_count = copy_directory(MEMORY_DIR, backup_dir / "memory", exclude + ["_backup_*", "archive"])
    if weekly:
        # フルバックアップはarchiveも含む
        archive_src = MEMORY_DIR / "archive"
        if archive_src.exists():
            mem_count += copy_directory(archive_src, backup_dir / "memory" / "archive", exclude)
    result["details"]["memory_files"] = mem_count

    # Qdrant
    print("  [2/6] Qdrant snapshot...")
    result["details"]["qdrant"] = qdrant_snapshot(backup_dir / "qdrant")

    # $CMEM
    print("  [3/6] $CMEM DB...")
    result["details"]["cmem"] = sqlite_backup(CMEM_DB, backup_dir / "cmem" / "claude-mem.db")

    # LightRAG
    print("  [4/6] LightRAG...")
    if LIGHTRAG_STORAGE.exists():
        lr_count = copy_directory(LIGHTRAG_STORAGE, backup_dir / "lightrag", exclude)
        result["details"]["lightrag_files"] = lr_count
    else:
        result["details"]["lightrag_files"] = 0

    # 設定ファイル
    print("  [5/6] 設定ファイル...")
    settings_dst = backup_dir / "settings"
    settings_dst.mkdir(parents=True, exist_ok=True)
    for target in BACKUP_TARGETS:
        if target["method"] == "copy_file" and target["path"].exists():
            shutil.copy2(str(target["path"]), str(settings_dst / target["path"].name))

    # チェックサム
    generate_checksums(backup_dir)

    # age暗号化
    enc_config = config.get("encryption", {})
    if enc_config.get("enabled") and enc_config.get("age_public_key"):
        print("  [6/6] age暗号化...")
        age_file = encrypt_with_age(backup_dir, enc_config["age_public_key"])
        if age_file:
            # 暗号化成功 → 元ディレクトリ削除
            shutil.rmtree(str(backup_dir))
            result["details"]["encrypted"] = True
            result["path"] = str(age_file)
            print(f"    暗号化完了: {age_file.name} ({age_file.stat().st_size // 1024}KB)")
        else:
            result["details"]["encrypted"] = False
            print("    暗号化失敗（平文で保存）")
    else:
        print("  [6/6] 暗号化スキップ（公開鍵未設定）")
        result["details"]["encrypted"] = False

    # 古いバックアップ削除
    max_age = nas_config.get("weekly_max_age_days", 90) if weekly else nas_config.get("daily_max_age_days", 30)
    subdir_path = nas_path / subdir
    removed = prune_old_backups(subdir_path, max_age, pattern="*")
    if removed:
        print(f"  古いバックアップ {removed} 件削除")

    print(f"  {level}完了")
    return result


def run_backup(local_only: bool = False, weekly: bool = False) -> dict:
    """バックアップ全体を実行."""
    config = load_config()
    state = load_state()
    start_time = time.time()
    results = {}

    # 初回は設定ファイルを生成
    if not CONFIG_FILE.exists():
        # age公開鍵を自動検出
        if AGE_KEY_FILE.exists():
            try:
                for line in AGE_KEY_FILE.read_text(encoding="utf-8").split("\n"):
                    if line.startswith("# public key:"):
                        config["encryption"]["age_public_key"] = line.split(": ", 1)[1].strip()
                        break
            except OSError:
                pass
        save_config(config)
        print(f"設定ファイル生成: {CONFIG_FILE}")

    # L0: ローカル
    if config.get("local", {}).get("enabled", True):
        results["local"] = run_local_backup(config)

    # L1/L2: NAS
    if not local_only:
        results["nas"] = run_nas_backup(config, weekly=weekly)

    duration = round(time.time() - start_time, 1)

    # 状態記録
    backup_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_sec": duration,
        "results": {k: v.get("status", "unknown") for k, v in results.items()},
        "type": "weekly" if weekly else "daily",
    }
    state.setdefault("backups", []).append(backup_entry)
    state["backups"] = state["backups"][-100:]  # 最新100件
    state["last_backup"] = backup_entry
    save_state(state)

    # Discord通知
    status_parts = []
    for k, v in results.items():
        s = v.get("status", "unknown")
        emoji = "✅" if s == "ok" else ("⏭️" if s in ("skipped", "disabled") else "❌")
        status_parts.append(f"{emoji} {k}: {s}")
    msg = f"💾 **Backup完了** ({duration}秒)\n" + "\n".join(status_parts)
    send_notification(msg)

    print(f"\n=== 完了 ({duration}秒) ===")
    return results


def show_status():
    """バックアップ状態を表示."""
    config = load_config()
    state = load_state()

    print("=== Backup Status ===")
    print(f"\n[設定]")
    print(f"  L0 ローカル: {'有効' if config.get('local', {}).get('enabled') else '無効'}")
    print(f"  L1/L2 NAS: {'有効' if config.get('nas', {}).get('enabled') else '無効'}")
    if config.get("nas", {}).get("path"):
        print(f"    パス: {config['nas']['path']}")
    print(f"  暗号化: {'有効' if config.get('encryption', {}).get('enabled') else '無効'}")
    if config.get("encryption", {}).get("age_public_key"):
        pk = config["encryption"]["age_public_key"]
        print(f"    公開鍵: {pk[:15]}...{pk[-8:]}")

    last = state.get("last_backup")
    if last:
        print(f"\n[最終バックアップ]")
        print(f"  日時: {last.get('timestamp', 'unknown')}")
        print(f"  タイプ: {last.get('type', 'unknown')}")
        print(f"  所要時間: {last.get('duration_sec', '?')}秒")
        print(f"  結果: {last.get('results', {})}")
    else:
        print("\n[最終バックアップ] なし")

    # ローカルバックアップ一覧
    backups = sorted(MEMORY_DIR.glob("_backup_*"))
    if backups:
        print(f"\n[ローカルバックアップ] {len(backups)}件")
        for b in backups[-5:]:
            age_days = (time.time() - b.stat().st_mtime) / 86400
            print(f"  {b.name} ({age_days:.0f}日前)")

    print(f"\n[設定ファイル] {CONFIG_FILE}")


def show_restore_guide():
    """復元ガイドを表示."""
    print("""
=== 復元ガイド ===

1. age暗号化バックアップの復号:
   age -d -i ~/.age/key.txt backup_YYYYMMDD.tar.age -o backup.tar
   tar xf backup.tar

2. チェックサム検証:
   cd backup_dir
   sha256sum -c checksums.sha256

3. memory/ 復元:
   cp -r backup/memory/*.md ~/.claude/projects/C--Development/memory/

4. Qdrant 復元:
   curl -X PUT "localhost:6333/collections/mem0_shared/snapshots/upload" \\
     -H "Content-Type: multipart/form-data" \\
     -F snapshot=@backup/qdrant/snapshot_file

5. $CMEM DB 復元:
   cp backup/cmem/claude-mem.db ~/.claude-mem/claude-mem.db

6. LightRAG 復元:
   cp -r backup/lightrag/* C:/Development/tools/lightrag-server/rag_storage/

7. 設定ファイル復元:
   cp backup/settings/settings.json ~/.claude/settings.json
   cp backup/settings/CLAUDE.md ~/.claude/CLAUDE.md

注意: Qdrant/LightRAGサーバーを停止してから復元し、復元後に再起動すること。
""")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        show_status()
    elif "--restore" in sys.argv:
        show_restore_guide()
    elif "--local-only" in sys.argv:
        run_backup(local_only=True)
    elif "--weekly" in sys.argv:
        run_backup(weekly=True)
    else:
        run_backup()
