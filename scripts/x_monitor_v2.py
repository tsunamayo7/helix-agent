"""X(Twitter) real-time monitoring via xAI OAuth — v2.

v1 used Ollama to hallucinate fake X posts.
v2 searches real X posts via Hermes Agent (xAI OAuth on Mac via Tailscale SSH),
then scores real results with Ollama.

Usage:
    python scripts/x_monitor_v2.py
    python scripts/x_monitor_v2.py --keywords "Claude Code,MCP"
    python scripts/x_monitor_v2.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [x_monitor_v2] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MAC_HOST = os.environ.get("HELIX_MAC_HOST", "localhost")
HERMES_PATH = os.environ.get("HERMES_MAC_PATH", "~/.local/bin/hermes")
INTEL_DIR = Path.home() / ".helix-agent" / "intelligence"
TOPICS_FILE = INTEL_DIR / "topics.json"
ROUTING_FILE = INTEL_DIR / "routing.json"
OUTPUT_DIR = INTEL_DIR / "collected"
LEGACY_OUTPUT_DIR = Path.home() / ".helix-agent" / "x_monitor"
HEARTBEAT_DIR = Path.home() / ".helix-agent" / "heartbeats"
WEBHOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "discord_webhook_fallback.py"
WINDOWS_HEALTH_URL = os.environ.get("HELIX_WIN_HEALTH", "http://100.78.22.44:8800")

DEFAULT_QUERIES = [
    "Claude Code MCP server new",
    "local LLM agent 2026",
    "AI VTuber streaming automation",
    "ComfyUI workflow",
    "MCP security vulnerability",
    "Ollama update release",
    "helix-agent OR helix-tools",
]

MAX_RESULTS_PER_QUERY = 3


def load_topics() -> list[dict]:
    """Load enabled topics from topics.json, falling back to DEFAULT_QUERIES."""
    if TOPICS_FILE.exists():
        try:
            data = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
            # Support both flat list and {topics: [...]} wrapper
            topic_list = data.get("topics", data) if isinstance(data, dict) else data
            topics = [t for t in topic_list if isinstance(t, dict) and t.get("enabled", True)]
            if topics:
                log.info("Loaded %d enabled topics from %s", len(topics), TOPICS_FILE)
                return topics
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read topics.json: %s", e)
    # Fallback: convert DEFAULT_QUERIES to topic dicts
    log.info("Using DEFAULT_QUERIES fallback (%d queries)", len(DEFAULT_QUERIES))
    return [
        {
            "id": f"default_{i}",
            "query": q,
            "category": "general",
            "priority": 5,
            "action_routes": ["discord"],
            "enabled": True,
        }
        for i, q in enumerate(DEFAULT_QUERIES)
    ]


def load_routing() -> dict:
    """Load routing rules from routing.json, falling back to defaults."""
    default = {"routes": {}, "default_min_score": 7}
    if ROUTING_FILE.exists():
        try:
            data = json.loads(ROUTING_FILE.read_text(encoding="utf-8"))
            log.info("Loaded routing from %s", ROUTING_FILE)
            data.setdefault("default_min_score", 7)
            data.setdefault("routes", {})
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read routing.json: %s", e)
    return default


def _hermes_local() -> str | None:
    """Return local hermes path if available, else None."""
    import shutil
    path = os.environ.get("HERMES_PATH") or shutil.which("hermes")
    if path and Path(path).exists():
        return path
    default = Path.home() / ".local" / "bin" / "hermes"
    return str(default) if default.exists() else None


async def search_x(query: str, max_results: int = 3) -> dict:
    """Search X via Hermes — local if available, SSH fallback."""
    prompt = f"Search X for: {query}. Return top {max_results} results with URLs."
    local = _hermes_local()
    if local:
        cmd = [local, "chat", "-q", prompt, "-t", "x_search"]
    else:
        remote_cmd = f"{HERMES_PATH} chat -q {shlex.quote(prompt)} -t x_search"
        cmd = [
            "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
            MAC_HOST, remote_cmd,
        ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            log.warning("x_search failed for '%s': %s", query, stderr.decode()[:200])
            return {"results": "", "success": False}
        output = stdout.decode("utf-8", errors="replace").strip()
        clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output).strip()
        return {"results": clean, "query": query, "success": bool(clean)}
    except asyncio.TimeoutError:
        log.warning("x_search timeout for '%s'", query)
        return {"results": "", "success": False}
    except Exception as e:
        log.warning("x_search error for '%s': %s", query, e)
        return {"results": "", "success": False}


ANALYZE_PROMPT = (
    "Analyze these X search results. Extract key findings as JSON array:\n"
    '[{"topic": "...", "summary": "...", "relevance_score": 1-10, '
    '"source_url": "..." or null, "author": "..." or null}]\n'
    "Score by relevance to: MCP servers, local LLMs, AI coding, VTuber, security. JSON only."
)


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b")


async def _ollama_chat(messages: list[dict], model: str = "") -> str:
    """Call Ollama chat API directly via HTTP."""
    import urllib.request
    url = f"{OLLAMA_URL}/api/chat"
    payload = json.dumps({
        "model": model or OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(
        None, lambda: urllib.request.urlopen(req, timeout=300).read(),
    )
    data = json.loads(resp)
    return data.get("message", {}).get("content", "")


async def analyze_results(
    raw_results: list[dict], topic_meta: dict | None = None,
) -> list[dict]:
    """Analyze raw Hermes results with Ollama to extract structured entries."""
    texts = [r["results"] for r in raw_results if r.get("success") and r["results"]]
    if not texts:
        return []
    combined = "\n---\n".join(texts)
    try:
        text = await _ollama_chat([
            {"role": "system", "content": ANALYZE_PROMPT},
            {"role": "user", "content": combined[:8000]},
        ])
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            entries = json.loads(match.group(0))
            if topic_meta:
                for e in entries:
                    e["topic_id"] = topic_meta.get("id")
                    e["category"] = topic_meta.get("category")
                    e["action_routes"] = topic_meta.get("action_routes", [])
            return entries
    except Exception as e:
        log.warning("Ollama analysis failed: %s", e)
    fallback = [{"topic": "raw", "summary": t[:200], "relevance_score": 5} for t in texts]
    if topic_meta:
        for e in fallback:
            e["topic_id"] = topic_meta.get("id")
            e["category"] = topic_meta.get("category")
            e["action_routes"] = topic_meta.get("action_routes", [])
    return fallback


def save_results(entries: list[dict]) -> Path:
    """Save scored entries to dated JSON file in collected directory."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = OUTPUT_DIR / f"{date_str}.json"
    existing = []
    if filepath.exists():
        try:
            existing = json.loads(filepath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    seen = {e.get("id") or e.get("text", "")[:50] for e in existing}
    new = []
    for e in entries:
        key = e.get("id") or e.get("text", e.get("content", ""))[:50]
        if key not in seen:
            e["collected_at"] = datetime.now(timezone.utc).isoformat()
            new.append(e)
            seen.add(key)
    if new:
        filepath.write_text(
            json.dumps(existing + new, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        log.info("Saved %d new entries (total: %d)", len(new), len(existing) + len(new))
    return filepath


def write_heartbeat() -> None:
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "daemon": "x_monitor", "pid": os.getpid(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "alive", "version": "v2",
    }
    (HEARTBEAT_DIR / "x_monitor.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )
    try:
        req = urllib.request.Request(
            f"{WINDOWS_HEALTH_URL}/heartbeat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def notify_discord(message: str) -> bool:
    if not WEBHOOK_SCRIPT.exists():
        return False
    try:
        return subprocess.run(
            [sys.executable, str(WEBHOOK_SCRIPT), message],
            capture_output=True, text=True, timeout=30,
        ).returncode == 0
    except Exception:
        return False


async def run_monitor(
    topics: list[dict], min_score: int = 7, dry_run: bool = False,
    routing: dict | None = None,
) -> list[dict]:
    write_heartbeat()
    routing = routing or {}
    all_high: list[dict] = []

    for topic in topics:
        query = topic["query"]
        topic_id = topic.get("id", "unknown")
        log.info("Searching [%s]: %s", topic_id, query)
        result = await search_x(query, MAX_RESULTS_PER_QUERY)
        if not result["success"]:
            log.info("  No results for '%s'", query)
            continue
        log.info("  Got results for '%s'", query)

        log.info("  Analyzing with Ollama...")
        entries = await analyze_results([result], topic_meta=topic)

        # Determine effective min_score from routing rules for this topic
        action_routes = topic.get("action_routes", [])
        routes_config = routing.get("routes", {})
        effective_min = min_score
        if action_routes and routes_config:
            route_scores = [
                routes_config[r].get("min_score", min_score)
                for r in action_routes if r in routes_config
            ]
            if route_scores:
                effective_min = min(route_scores)

        high = [e for e in entries if e.get("relevance_score", 0) >= effective_min]
        log.info("  High relevance: %d/%d (score >= %d)", len(high), len(entries), effective_min)
        all_high.extend(high)

    if not all_high:
        log.info("No high-relevance results across all topics")
        write_heartbeat()
        return []

    if dry_run:
        for e in all_high:
            log.info("  [%d] %s: %s", e.get("relevance_score", 0),
                     e.get("topic", "?"), e.get("summary", "")[:100])
        write_heartbeat()
        return all_high

    save_results(all_high)
    summary = f"X Monitor v2: {len(all_high)} relevant findings\n"
    for e in all_high[:3]:
        summary += f"- [{e.get('relevance_score', '?')}] {e.get('topic', '?')}: {e.get('summary', '')[:80]}\n"
    notify_discord(summary)

    write_heartbeat()
    return all_high


def main():
    parser = argparse.ArgumentParser(description="X Monitor v2")
    parser.add_argument("--keywords", type=str, help="Comma-separated queries")
    parser.add_argument("--min-score", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    routing = load_routing()

    if args.keywords:
        # Backward compat: create synthetic topic dicts from CLI keywords
        topics = [
            {
                "id": f"cli_{i}",
                "query": q.strip(),
                "category": "cli",
                "priority": 5,
                "action_routes": ["discord"],
                "enabled": True,
            }
            for i, q in enumerate(args.keywords.split(","))
        ]
    else:
        topics = load_topics()

    results = asyncio.run(run_monitor(
        topics, args.min_score, args.dry_run, routing=routing,
    ))
    log.info("Done. %d high-relevance posts.", len(results))


if __name__ == "__main__":
    main()
