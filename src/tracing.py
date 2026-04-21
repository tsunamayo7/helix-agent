"""Tracing and observability for agent loops."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class TraceEntry:
    timestamp: str
    step: int
    type: str  # llm_call | tool_result
    data: dict


@dataclass
class TraceSummary:
    total_steps: int
    total_tokens_in: int
    total_tokens_out: int
    total_duration_ms: float
    tool_stats: dict[str, dict]  # tool_name -> {calls, successes, failures, total_ms}

    def to_dict(self) -> dict:
        return {
            "total_steps": self.total_steps,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "total_duration_ms": round(self.total_duration_ms, 1),
            "tool_stats": self.tool_stats,
        }


class TraceRecorder:
    """Record JSONL traces for agent loop steps."""

    def __init__(self, task_id: str = "", trace_dir: Path | None = None):
        self.task_id = task_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._trace_dir = trace_dir or (Path.home() / ".helix-agent" / "traces")
        self._entries: list[TraceEntry] = []
        self._start_time = time.monotonic()

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def record_llm_call(
        self,
        step: int,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: float,
        thought: str = "",
        action: str = "",
        action_input: str = "",
    ) -> None:
        self._entries.append(TraceEntry(
            timestamp=self._now_iso(),
            step=step,
            type="llm_call",
            data={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "duration_ms": round(duration_ms, 1),
                "thought": thought[:500],
                "action": action,
                "action_input": str(action_input)[:500],
            },
        ))

    def record_tool_result(
        self,
        step: int,
        tool: str,
        duration_ms: float,
        success: bool,
        result_length: int = 0,
    ) -> None:
        self._entries.append(TraceEntry(
            timestamp=self._now_iso(),
            step=step,
            type="tool_result",
            data={
                "tool": tool,
                "duration_ms": round(duration_ms, 1),
                "success": success,
                "result_length": result_length,
            },
        ))

    def summary(self) -> TraceSummary:
        total_in = 0
        total_out = 0
        max_step = 0
        tool_stats: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, "successes": 0, "failures": 0, "total_ms": 0.0}
        )

        for entry in self._entries:
            max_step = max(max_step, entry.step)
            if entry.type == "llm_call":
                total_in += entry.data.get("input_tokens", 0)
                total_out += entry.data.get("output_tokens", 0)
            elif entry.type == "tool_result":
                name = entry.data.get("tool", "unknown")
                stats = tool_stats[name]
                stats["calls"] += 1
                if entry.data.get("success"):
                    stats["successes"] += 1
                else:
                    stats["failures"] += 1
                stats["total_ms"] += entry.data.get("duration_ms", 0)

        # Round tool ms
        for stats in tool_stats.values():
            stats["total_ms"] = round(stats["total_ms"], 1)

        elapsed = (time.monotonic() - self._start_time) * 1000
        return TraceSummary(
            total_steps=max_step,
            total_tokens_in=total_in,
            total_tokens_out=total_out,
            total_duration_ms=elapsed,
            tool_stats=dict(tool_stats),
        )

    def save(self) -> Path | None:
        """Write JSONL trace file. Returns path or None if no entries."""
        if not self._entries:
            return None
        try:
            self._trace_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{self.task_id}_{ts}.jsonl"
        path = self._trace_dir / filename

        try:
            with open(path, "w", encoding="utf-8") as f:
                for entry in self._entries:
                    line = {
                        "timestamp": entry.timestamp,
                        "step": entry.step,
                        "type": entry.type,
                        **entry.data,
                    }
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
            return path
        except OSError:
            return None
