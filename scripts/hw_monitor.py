"""ハードウェア監視 — VRAM/GPU温度/CPU温度を定期チェックし異常時に通知."""

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

ALERT_THRESHOLDS = {
    "gpu_temp_warn": 80,       # GPU温度 警告 (℃)
    "gpu_temp_critical": 90,   # GPU温度 危険 (℃)
    "vram_usage_warn": 90,     # VRAM使用率 警告 (%)
    "vram_usage_critical": 95, # VRAM使用率 危険 (%)
    "cpu_temp_warn": 85,       # CPU温度 警告 (℃)
    "cpu_temp_critical": 95,   # CPU温度 危険 (℃)
}

LOG_DIR = Path.home() / ".helix-agent" / "hw_monitor"
LOG_FILE = LOG_DIR / "hw_status.json"
ALERT_FILE = LOG_DIR / "alerts.jsonl"


def get_gpu_status() -> list[dict]:
    """nvidia-smiからGPU情報を取得."""
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,utilization.gpu,fan.speed,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []

        gpus = []
        for line in result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                mem_used = float(parts[3])
                mem_total = float(parts[4])
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temp_c": int(parts[2]) if parts[2] != "[N/A]" else None,
                    "vram_used_mb": int(mem_used),
                    "vram_total_mb": int(mem_total),
                    "vram_usage_pct": round(mem_used / mem_total * 100, 1) if mem_total > 0 else 0,
                    "gpu_util_pct": int(parts[5]) if parts[5] != "[N/A]" else None,
                    "fan_pct": parts[6] if len(parts) > 6 else None,
                    "power_w": parts[7] if len(parts) > 7 else None,
                })
        return gpus
    except Exception:
        return []


def get_cpu_info() -> dict:
    """CPU使用率・RAM使用率・CPU温度を取得."""
    info = {"temp_c": None, "usage_pct": None, "ram_used_mb": None, "ram_total_mb": None, "ram_pct": None}

    try:
        import psutil
        info["usage_pct"] = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        info["ram_used_mb"] = mem.used // 1024 // 1024
        info["ram_total_mb"] = mem.total // 1024 // 1024
        info["ram_pct"] = mem.percent
    except ImportError:
        pass

    # CPU温度: LibreHardwareMonitor / OpenHardwareMonitor経由
    for ns in ["root/LibreHardwareMonitor", "root/OpenHardwareMonitor"]:
        try:
            result = subprocess.run(
                ["powershell", "-Command",
                 f"Get-CimInstance -Namespace {ns} -ClassName Sensor 2>$null | "
                 "Where-Object { $_.SensorType -eq 'Temperature' -and $_.Name -match 'CPU' } | "
                 "Select-Object -First 1 -ExpandProperty Value"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                info["temp_c"] = round(float(result.stdout.strip()), 1)
                break
        except Exception:
            pass

    return info


def check_alerts(gpus: list[dict], cpu_temp: float | None) -> list[dict]:
    """閾値チェックしてアラートを生成."""
    alerts = []
    t = ALERT_THRESHOLDS

    for gpu in gpus:
        # GPU温度
        if gpu["temp_c"] is not None:
            if gpu["temp_c"] >= t["gpu_temp_critical"]:
                alerts.append({
                    "level": "CRITICAL",
                    "type": "gpu_temp",
                    "gpu": gpu["index"],
                    "value": gpu["temp_c"],
                    "threshold": t["gpu_temp_critical"],
                    "message": f"GPU{gpu['index']} ({gpu['name']}) 温度が危険: {gpu['temp_c']}℃",
                })
            elif gpu["temp_c"] >= t["gpu_temp_warn"]:
                alerts.append({
                    "level": "WARNING",
                    "type": "gpu_temp",
                    "gpu": gpu["index"],
                    "value": gpu["temp_c"],
                    "threshold": t["gpu_temp_warn"],
                    "message": f"GPU{gpu['index']} ({gpu['name']}) 温度が高い: {gpu['temp_c']}℃",
                })

        # VRAM使用率
        if gpu["vram_usage_pct"] >= t["vram_usage_critical"]:
            alerts.append({
                "level": "CRITICAL",
                "type": "vram_usage",
                "gpu": gpu["index"],
                "value": gpu["vram_usage_pct"],
                "threshold": t["vram_usage_critical"],
                "message": f"GPU{gpu['index']} VRAM使用率が危険: {gpu['vram_usage_pct']}% ({gpu['vram_used_mb']}MB/{gpu['vram_total_mb']}MB)",
            })
        elif gpu["vram_usage_pct"] >= t["vram_usage_warn"]:
            alerts.append({
                "level": "WARNING",
                "type": "vram_usage",
                "gpu": gpu["index"],
                "value": gpu["vram_usage_pct"],
                "threshold": t["vram_usage_warn"],
                "message": f"GPU{gpu['index']} VRAM使用率が高い: {gpu['vram_usage_pct']}%",
            })

    # CPU温度
    cpu_temp = cpu_info.get("temp_c") if cpu_info else None
    if cpu_temp is not None:
        if cpu_temp >= t["cpu_temp_critical"]:
            alerts.append({
                "level": "CRITICAL",
                "type": "cpu_temp",
                "value": cpu_temp,
                "threshold": t["cpu_temp_critical"],
                "message": f"CPU温度が危険: {cpu_temp}℃",
            })
        elif cpu_temp >= t["cpu_temp_warn"]:
            alerts.append({
                "level": "WARNING",
                "type": "cpu_temp",
                "value": cpu_temp,
                "threshold": t["cpu_temp_warn"],
                "message": f"CPU温度が高い: {cpu_temp}℃",
            })

    # RAM使用率
    ram_pct = cpu_info.get("ram_pct") if cpu_info else None
    if ram_pct is not None and ram_pct >= 90:
        alerts.append({
            "level": "WARNING",
            "type": "ram_usage",
            "value": ram_pct,
            "message": f"RAM使用率が高い: {ram_pct}% ({cpu_info.get('ram_used_mb')}MB/{cpu_info.get('ram_total_mb')}MB)",
        })

    return alerts


def save_status(gpus: list[dict], cpu_info: dict | None, alerts: list[dict]) -> None:
    """現在のステータスをJSONに保存."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    status = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gpus": gpus,
        "cpu": cpu_info or {},
        "alerts": alerts,
        "alert_count": len(alerts),
    }

    LOG_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    # アラートがあればJSONLに追記
    if alerts:
        with open(ALERT_FILE, "a", encoding="utf-8") as f:
            for alert in alerts:
                alert["timestamp"] = status["timestamp"]
                f.write(json.dumps(alert, ensure_ascii=False) + "\n")


def get_latest_status() -> dict | None:
    """最新のステータスを読み込み（Claudeセッションから呼ぶ用）."""
    if not LOG_FILE.exists():
        return None
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def format_status(status: dict) -> str:
    """ステータスを人間が読める形式にフォーマット."""
    lines = [f"=== HW Monitor ({status['timestamp'][:19]}) ==="]

    for gpu in status.get("gpus", []):
        temp = f"{gpu['temp_c']}℃" if gpu['temp_c'] is not None else "N/A"
        lines.append(
            f"  GPU{gpu['index']} {gpu['name']}: {temp} | "
            f"VRAM {gpu['vram_used_mb']}MB/{gpu['vram_total_mb']}MB ({gpu['vram_usage_pct']}%) | "
            f"GPU使用率 {gpu.get('gpu_util_pct', 'N/A')}%"
        )

    cpu = status.get("cpu", {})
    cpu_temp = cpu.get("temp_c")
    cpu_usage = cpu.get("usage_pct")
    ram_pct = cpu.get("ram_pct")
    ram_used = cpu.get("ram_used_mb")
    ram_total = cpu.get("ram_total_mb")
    cpu_parts = []
    if cpu_temp is not None:
        cpu_parts.append(f"{cpu_temp}℃")
    if cpu_usage is not None:
        cpu_parts.append(f"使用率 {cpu_usage}%")
    lines.append(f"  CPU: {' | '.join(cpu_parts)}" if cpu_parts else "  CPU: 情報なし")
    if ram_pct is not None:
        lines.append(f"  RAM: {ram_used}MB/{ram_total}MB ({ram_pct}%)")

    if status.get("alerts"):
        lines.append(f"\n  *** アラート {len(status['alerts'])}件 ***")
        for a in status["alerts"]:
            lines.append(f"  [{a['level']}] {a['message']}")
    else:
        lines.append("  状態: 正常")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        status = get_latest_status()
        if status:
            print(format_status(status))
        else:
            print("ステータスファイルなし。hw_monitor.py を一度実行してください。")
    elif len(sys.argv) > 1 and sys.argv[1] == "watch":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        print(f"監視開始 (間隔: {interval}秒)")
        while True:
            gpus = get_gpu_status()
            cpu_temp = get_cpu_temp()
            alerts = check_alerts(gpus, cpu_temp)
            save_status(gpus, cpu_temp, alerts)
            status = get_latest_status()
            if status:
                print(format_status(status))
            if alerts:
                print("*** アラート検出 ***")
            time.sleep(interval)
    else:
        # 1回実行（タスクスケジューラ用）
        gpus = get_gpu_status()
        cpu_info = get_cpu_info()
        alerts = check_alerts(gpus, cpu_info)
        save_status(gpus, cpu_info, alerts)
        # ハートビート送信
        try:
            from supervisor import write_heartbeat
            write_heartbeat("hw_monitor", {"alert_count": len(alerts)})
        except ImportError:
            pass
        if alerts:
            for a in alerts:
                print(f"[{a['level']}] {a['message']}")
        else:
            print("OK")
