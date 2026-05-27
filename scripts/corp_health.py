#!/usr/bin/env python3
"""Corp Health Check — 全サービス・記憶・VLM の統合ヘルスチェック.

Usage:
    python3 scripts/corp_health.py           # フルチェック
    python3 scripts/corp_health.py --quick   # 接続のみ
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
OLLAMA_HOST = os.environ.get("HELIX_OLLAMA_HOST", "http://localhost:11434")
HEALTH_URL = os.environ.get("HELIX_HEALTH_URL", "http://localhost:8800")
COLLECTIONS = ["dept_build", "dept_research", "dept_design", "dept_qa", "dept_hr", "mem0_shared"]


def check_service(name, url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def check_qdrant():
    results = {}
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections", headers={"api-key": QDRANT_API_KEY})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            colls = data.get("result", {}).get("collections", [])
            results["connected"] = True
            results["collections"] = len(colls)
    except Exception as e:
        results["connected"] = False
        results["error"] = str(e)
        return results

    results["details"] = {}
    for coll in COLLECTIONS:
        try:
            req = urllib.request.Request(f"{QDRANT_URL}/collections/{coll}", headers={"api-key": QDRANT_API_KEY})
            with urllib.request.urlopen(req, timeout=5) as r:
                info = json.loads(r.read()).get("result", {})
                results["details"][coll] = {
                    "status": info.get("status", "?"),
                    "points": info.get("points_count", 0),
                }
        except Exception:
            results["details"][coll] = {"status": "error", "points": 0}
    return results


def check_ollama():
    results = {}
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
            models = data.get("models", [])
            results["connected"] = True
            results["models"] = len(models)
            results["vlm_235b"] = any("235" in m["name"] for m in models)
            results["vlm_32b"] = any("qwen3-vl" in m["name"] and "235" not in m["name"] for m in models)
    except Exception as e:
        results["connected"] = False
        results["error"] = str(e)
    return results


def check_memory_files():
    mem_dir = Path.home() / ".claude" / "projects" / "Development" / "memory"
    files = list(mem_dir.glob("*.md"))
    events_log = Path.home() / ".claude" / "memory_events.jsonl"
    events_count = sum(1 for _ in open(events_log)) if events_log.exists() else 0
    spool_dir = Path.home() / ".claude" / "qdrant_spool"
    spool_count = sum(1 for f in spool_dir.glob("spool_*.jsonl") for _ in open(f)) if spool_dir.exists() else 0
    return {
        "memory_files": len(files),
        "canonical_events": events_count,
        "pending_spool": spool_count,
    }


def main():
    parser = argparse.ArgumentParser(description="Corp Health Check")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = {"timestamp": ts, "services": {}, "qdrant": {}, "ollama": {}, "memory": {}}

    # Services
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections", headers={"api-key": QDRANT_API_KEY})
        with urllib.request.urlopen(req, timeout=5):
            report["services"]["qdrant"] = True
    except Exception:
        report["services"]["qdrant"] = False
    report["services"]["ollama"] = check_service("Ollama", f"{OLLAMA_HOST}/api/tags")
    report["services"]["health_server"] = check_service("Health", f"{HEALTH_URL}/health")

    if not args.quick:
        report["qdrant"] = check_qdrant()
        report["ollama"] = check_ollama()
        report["memory"] = check_memory_files()

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # Pretty print
    print(f"=== Corp Health Check ({ts}) ===\n")

    print("Services:")
    for svc, ok in report["services"].items():
        print(f"  {'[OK]' if ok else '[NG]'} {svc}")

    if not args.quick:
        print("\nQdrant:")
        q = report["qdrant"]
        if q.get("connected"):
            total = 0
            for coll, info in q.get("details", {}).items():
                status = info["status"]
                points = info["points"]
                total += points
                icon = "OK" if status == "green" else "WARN"
                print(f"  [{icon}] {coll}: {points:,} points ({status})")
            print(f"  Total: {total:,} points")
        else:
            print(f"  [NG] {q.get('error', 'unreachable')}")

        print("\nOllama:")
        o = report["ollama"]
        if o.get("connected"):
            print(f"  Models: {o['models']}")
            print(f"  235B VLM: {'YES' if o['vlm_235b'] else 'NO'}")
            print(f"  32B VLM: {'YES' if o['vlm_32b'] else 'NO'}")
        else:
            print(f"  [NG] {o.get('error', 'unreachable')}")

        print("\nMemory:")
        m = report["memory"]
        print(f"  Memory files: {m['memory_files']}")
        print(f"  Canonical events: {m['canonical_events']}")
        print(f"  Pending spool: {m['pending_spool']}")

    # Overall
    all_ok = all(report["services"].values())
    print(f"\nOverall: {'HEALTHY' if all_ok else 'DEGRADED'}")


if __name__ == "__main__":
    main()
