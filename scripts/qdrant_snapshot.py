#!/usr/bin/env python3
"""Qdrant 日次スナップショット + 整合性検証.

Usage:
    python3 scripts/qdrant_snapshot.py                  # スナップショット取得
    python3 scripts/qdrant_snapshot.py --list            # 既存スナップショット一覧
    python3 scripts/qdrant_snapshot.py --verify          # 整合性検証
"""

import argparse
import json
import os
import urllib.request
from datetime import datetime

QDRANT_URL = os.environ.get("QDRANT_URL", "http://tsunamayo-1:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
HEADERS = {"api-key": QDRANT_API_KEY, "Content-Type": "application/json"}
COLLECTIONS = ["dept_build", "dept_research", "dept_design", "dept_qa", "dept_hr", "mem0_shared"]


def api_get(path):
    req = urllib.request.Request(f"{QDRANT_URL}{path}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def api_post(path, data=None):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(f"{QDRANT_URL}{path}", data=body, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def create_snapshots():
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    print(f"=== Qdrant Snapshot ({ts}) ===")
    for coll in COLLECTIONS:
        try:
            points = api_get(f"/collections/{coll}").get("result", {}).get("points_count", 0)
            snap = api_post(f"/collections/{coll}/snapshots").get("result", {}).get("name", "?")
            print(f"  [{coll}] {points} points -> {snap}")
        except Exception as e:
            print(f"  [{coll}] ERROR: {e}")


def list_snapshots():
    for coll in COLLECTIONS:
        try:
            snaps = api_get(f"/collections/{coll}/snapshots").get("result", [])
            print(f"  [{coll}] {len(snaps)} snapshots")
            for s in snaps[-3:]:
                print(f"    {s.get('name','?')} ({s.get('size',0)//1024//1024}MB)")
        except Exception as e:
            print(f"  [{coll}] ERROR: {e}")


def verify():
    ok = 0
    for coll in COLLECTIONS:
        try:
            r = api_get(f"/collections/{coll}").get("result", {})
            print(f"  [{coll}] status={r.get('status','?')} points={r.get('points_count',0)}")
            if r.get("status") == "green" and r.get("points_count", 0) > 0:
                ok += 1
        except Exception as e:
            print(f"  [{coll}] ERROR: {e}")
    print(f"Healthy: {ok}/{len(COLLECTIONS)}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true")
    p.add_argument("--verify", action="store_true")
    args = p.parse_args()
    if args.list:
        list_snapshots()
    elif args.verify:
        verify()
    else:
        create_snapshots()
