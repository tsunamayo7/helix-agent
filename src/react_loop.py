"""ReAct agent loop: Reason -> Action -> Observe -> Reflect."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .ollama_client import OllamaClient
from .tools import ToolRegistry
from .tracing import TraceRecorder

logger = logging.getLogger(__name__)

MAX_CONCURRENT_TOOLS = 10
DEFAULT_STEP_TIMEOUT = 120
DEFAULT_TOTAL_TIMEOUT = 600
CONTEXT_COMPRESS_RATIO = 0.75  # compress when messages exceed this ratio of context length
OOM_PATTERNS = ("out of memory", "oom", "cuda error", "vram", "gpu memory")
SMALLER_MODEL_FALLBACKS = {
    "qwen3.5:122b": "qwen3:32b",
    "qwen3:32b": "qwen3:14b",
    "qwen3:14b": "qwen3:8b",
    "gemma4:31b": "gemma3:27b",
    "gemma3:27b": "gemma3:12b",
    "gemma3:12b": "gemma3:4b",
    "llama3.3:70b": "llama3.1:8b",
    "deepseek-r1:70b": "deepseek-r1:14b",
    "deepseek-r1:32b": "deepseek-r1:14b",
    "nemotron-cascade-2": "qwen3:14b",
}


@dataclass
class AgentStep:
    """One step of the agent loop."""

    step: int
    thought: str
    action: str
    action_input: str
    observation: str = ""


@dataclass
class AgentResult:
    """Final result of the agent loop."""

    answer: str
    model: str
    steps: list[AgentStep] = field(default_factory=list)
    finished: bool = True  # False if max_steps reached
    trace_summary: dict | None = None

    def to_dict(self) -> dict:
        d = {
            "answer": self.answer,
            "model": self.model,
            "steps": len(self.steps),
            "finished": self.finished,
            "trace": [
                {
                    "step": s.step,
                    "thought": s.thought[:200],
                    "action": s.action,
                    "observation": s.observation[:200] if s.observation else "",
                }
                for s in self.steps
            ],
        }
        if self.trace_summary:
            d["trace_summary"] = self.trace_summary
        return d


REACT_SYSTEM_PROMPT = """\
You are a precise, tool-using assistant. Solve the task step by step.

## Principles
- Never delegate understanding: read and verify results yourself.
- Think before acting. Each step: Observe context -> Think about approach -> Act -> Verify result.
- If a tool fails, try an alternative approach instead of retrying the same way.
- When multiple independent lookups are needed, state them all (they may run in parallel).
- Be concise. No filler text.

## Tools
{tools}

## Response Format
Respond with ONLY this JSON (no markdown, no extra text):
{{"thought": "brief reasoning", "action": "tool_name", "action_input": "input"}}

When done:
{{"thought": "summary of findings", "action": "finish", "action_input": "final answer"}}"""


class ReactLoop:
    """Execute a ReAct (Reason-Act) loop with a local LLM."""

    def __init__(
        self,
        client: OllamaClient,
        tools: ToolRegistry,
        max_steps: int = 10,
        max_retries: int = 2,
        use_native_tools: bool = False,
        stream: bool = False,
        step_timeout_sec: int = DEFAULT_STEP_TIMEOUT,
        total_timeout_sec: int = DEFAULT_TOTAL_TIMEOUT,
    ):
        self.client = client
        self.tools = tools
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.use_native_tools = use_native_tools
        self.stream = stream
        self.step_timeout_sec = step_timeout_sec
        self.total_timeout_sec = total_timeout_sec

    async def run(
        self,
        task: str,
        model: str,
        *,
        context: str = "",
        temperature: float = 0.1,
        on_progress: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> AgentResult:
        """Run the ReAct loop until finish or max_steps."""
        tracer = TraceRecorder(task_id=task[:30].replace(" ", "_"))
        system = REACT_SYSTEM_PROMPT.format(tools=self.tools.format_for_prompt())

        messages: list[dict] = [
            {"role": "system", "content": system},
        ]

        user_content = task
        if context:
            user_content = f"{task}\n\nContext:\n{context}"
        messages.append({"role": "user", "content": user_content})

        steps: list[AgentStep] = []
        current_model = model
        total_start = time.monotonic()

        if self.use_native_tools:
            result = await self._run_native_tools(
                current_model, messages, temperature, steps, on_progress, tracer
            )
            self._finalize_trace(tracer, result)
            return result

        for step_num in range(1, self.max_steps + 1):
            # Total timeout check
            if self._total_timeout_exceeded(total_start):
                return self._make_partial_result(
                    current_model, steps, "Total timeout exceeded", tracer
                )

            # Compress history if approaching context limit
            await self._maybe_compress_history(messages, current_model)

            # Get LLM response with step timeout
            t0 = time.monotonic()
            try:
                response, input_tokens, output_tokens = await asyncio.wait_for(
                    self._call_llm_with_usage(current_model, messages, temperature),
                    timeout=self.step_timeout_sec,
                )
            except asyncio.TimeoutError:
                return self._make_partial_result(
                    current_model, steps, f"Step {step_num} timed out", tracer
                )
            except Exception as exc:
                exc_msg = str(exc).lower()
                if any(p in exc_msg for p in OOM_PATTERNS):
                    fallback = SMALLER_MODEL_FALLBACKS.get(current_model)
                    if fallback:
                        logger.warning("OOM on %s, falling back to %s", current_model, fallback)
                        current_model = fallback
                        continue
                return self._make_partial_result(
                    current_model, steps, f"LLM error: {exc}", tracer
                )
            llm_ms = (time.monotonic() - t0) * 1000

            # Parse the action
            parsed = self._parse_response(response)
            if parsed is None:
                parsed = await self._retry_parse_enhanced(
                    current_model, messages, response, temperature
                )
                if parsed is None:
                    return self._make_partial_result(
                        current_model, steps, response, tracer
                    )

            thought = parsed.get("thought", "")
            action = parsed.get("action", "finish")
            action_input = parsed.get("action_input", "")

            tracer.record_llm_call(
                step=step_num, model=current_model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                duration_ms=llm_ms, thought=thought, action=action,
                action_input=str(action_input),
            )

            step = AgentStep(
                step=step_num, thought=thought,
                action=action, action_input=str(action_input),
            )

            if action == "finish":
                step.observation = "(finished)"
                steps.append(step)
                result = AgentResult(
                    answer=str(action_input), model=current_model,
                    steps=steps, finished=True,
                )
                self._finalize_trace(tracer, result)
                return result

            if on_progress:
                await on_progress(step_num, self.max_steps, action)

            # Execute tool with timeout
            t0 = time.monotonic()
            try:
                observation = await asyncio.wait_for(
                    self.tools.execute(action, str(action_input)),
                    timeout=self.step_timeout_sec,
                )
                tool_success = "Error" not in observation[:50]
            except asyncio.TimeoutError:
                observation = f"Error: Tool '{action}' timed out after {self.step_timeout_sec}s"
                tool_success = False
            except Exception as exc:
                observation = f"Error executing {action}: {exc}"
                tool_success = False
            tool_ms = (time.monotonic() - t0) * 1000

            tracer.record_tool_result(
                step=step_num, tool=action, duration_ms=tool_ms,
                success=tool_success, result_length=len(observation),
            )

            step.observation = observation
            steps.append(step)

            messages.append({"role": "assistant", "content": response})

            if tool_success:
                messages.append({
                    "role": "user",
                    "content": f"Observation: {observation}\n\nContinue with the next step.",
                })
            else:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Observation (ERROR): {observation}\n\n"
                        "The tool failed. Try a different approach or tool. "
                        "Do NOT retry the same action with identical input."
                    ),
                })

        result = AgentResult(
            answer=self._extract_partial_answer(steps),
            model=current_model, steps=steps, finished=False,
        )
        self._finalize_trace(tracer, result)
        return result

    async def _run_native_tools(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        steps: list[AgentStep],
        on_progress: Callable[[int, int, str], Awaitable[None]] | None,
        tracer: TraceRecorder,
    ) -> AgentResult:
        """Run with Ollama native function calling (tools parameter)."""
        total_start = time.monotonic()

        for step_num in range(1, self.max_steps + 1):
            if self._total_timeout_exceeded(total_start):
                return self._make_partial_result(model, steps, "Total timeout exceeded", tracer)

            t0 = time.monotonic()
            try:
                msg = await asyncio.wait_for(
                    self._call_llm_with_tools(model, messages, temperature),
                    timeout=self.step_timeout_sec,
                )
            except asyncio.TimeoutError:
                return self._make_partial_result(model, steps, f"Step {step_num} timed out", tracer)
            except Exception as exc:
                exc_msg = str(exc).lower()
                if any(p in exc_msg for p in OOM_PATTERNS):
                    fallback = SMALLER_MODEL_FALLBACKS.get(model)
                    if fallback:
                        logger.warning("OOM on %s, falling back to %s", model, fallback)
                        model = fallback
                        continue
                return self._make_partial_result(model, steps, f"LLM error: {exc}", tracer)
            llm_ms = (time.monotonic() - t0) * 1000

            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            if not tool_calls:
                tracer.record_llm_call(
                    step=step_num, model=model,
                    input_tokens=0, output_tokens=0,
                    duration_ms=llm_ms, thought=content, action="finish",
                )
                step = AgentStep(
                    step=step_num, thought=content,
                    action="finish", action_input=content,
                    observation="(finished)",
                )
                steps.append(step)
                return AgentResult(
                    answer=content, model=model, steps=steps, finished=True,
                )

            tracer.record_llm_call(
                step=step_num, model=model,
                input_tokens=0, output_tokens=0,
                duration_ms=llm_ms, thought=content,
                action=",".join(
                    tc.get("function", {}).get("name", "") for tc in tool_calls
                ),
            )

            messages.append(msg)

            parsed_calls = []
            for tc in tool_calls:
                func = tc.get("function", {})
                action = func.get("name", "")
                args = func.get("arguments", {})
                if isinstance(args, dict):
                    action_input = str(next(iter(args.values()))) if len(args) == 1 else json.dumps(args)
                else:
                    action_input = str(args)
                tool_obj = self.tools.get(action)
                is_ro = tool_obj.is_read_only if tool_obj else False
                parsed_calls.append((action, action_input, is_ro))

            read_only_calls = [(a, ai) for a, ai, ro in parsed_calls if ro]
            write_calls = [(a, ai) for a, ai, ro in parsed_calls if not ro]

            if read_only_calls:
                sem = asyncio.Semaphore(MAX_CONCURRENT_TOOLS)

                async def _exec_ro(act: str, inp: str) -> tuple[str, str, str]:
                    async with sem:
                        obs = await self.tools.execute(act, inp)
                    return act, inp, obs

                ro_results = await asyncio.gather(
                    *[_exec_ro(a, ai) for a, ai in read_only_calls]
                )
                for action, action_input, observation in ro_results:
                    step = AgentStep(
                        step=step_num, thought=content,
                        action=action, action_input=action_input,
                        observation=observation,
                    )
                    steps.append(step)
                    messages.append({"role": "tool", "content": observation})
                    tracer.record_tool_result(
                        step=step_num, tool=action, duration_ms=0,
                        success="Error" not in observation[:50],
                        result_length=len(observation),
                    )
                    if on_progress:
                        await on_progress(step_num, self.max_steps, action)

            for action, action_input in write_calls:
                step = AgentStep(
                    step=step_num, thought=content,
                    action=action, action_input=action_input,
                )
                if on_progress:
                    await on_progress(step_num, self.max_steps, action)
                t0 = time.monotonic()
                try:
                    observation = await asyncio.wait_for(
                        self.tools.execute(action, action_input),
                        timeout=self.step_timeout_sec,
                    )
                    tool_success = "Error" not in observation[:50]
                except asyncio.TimeoutError:
                    observation = f"Error: Tool '{action}' timed out"
                    tool_success = False
                tool_ms = (time.monotonic() - t0) * 1000
                step.observation = observation
                steps.append(step)
                messages.append({"role": "tool", "content": observation})
                tracer.record_tool_result(
                    step=step_num, tool=action, duration_ms=tool_ms,
                    success=tool_success, result_length=len(observation),
                )

        return AgentResult(
            answer=self._extract_partial_answer(steps),
            model=model, steps=steps, finished=False,
        )

    async def _call_llm_with_usage(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
    ) -> tuple[str, int, int]:
        """Call LLM and return (text, input_tokens, output_tokens)."""
        if self.stream:
            chunks: list[str] = []
            async for chunk in self.client.chat_stream(
                model=model, messages=messages,
                temperature=temperature, format_json=True,
            ):
                chunks.append(chunk)
            return "".join(chunks), 0, 0

        resp = await self.client.chat_with_usage(
            model=model, messages=messages,
            temperature=temperature, format_json=True,
        )
        return resp.content, resp.input_tokens, resp.output_tokens

    async def _call_llm(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
    ) -> str:
        """Call the LLM and return the response text (backward compat)."""
        text, _, _ = await self._call_llm_with_usage(model, messages, temperature)
        return text

    async def _call_llm_with_tools(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
    ) -> dict:
        return await self.client.chat_with_tools(
            model=model, messages=messages,
            tools=self.tools.to_ollama_tools(), temperature=temperature,
        )

    def _parse_response(self, response: str) -> dict | None:
        """Parse the LLM response as JSON with thought/action/action_input."""
        text = response.strip()

        if "```" in text:
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{[^{}]*\}", text)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        if "action" not in data:
            return None

        return {
            "thought": data.get("thought", ""),
            "action": data["action"],
            "action_input": data.get("action_input", ""),
        }

    async def _retry_parse_enhanced(
        self,
        model: str,
        messages: list[dict],
        bad_response: str,
        temperature: float,
    ) -> dict | None:
        """Enhanced retry: structured retries then partial text extraction."""
        for attempt in range(self.max_retries):
            retry_messages = messages + [
                {"role": "assistant", "content": bad_response},
                {
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. "
                        'Respond with ONLY: {"thought": "...", "action": "tool_name", "action_input": "..."}'
                    ),
                },
            ]
            try:
                response = await asyncio.wait_for(
                    self._call_llm(model, retry_messages, temperature),
                    timeout=self.step_timeout_sec,
                )
            except (asyncio.TimeoutError, Exception):
                continue
            parsed = self._parse_response(response)
            if parsed:
                return parsed

        # Last resort: extract partial meaning from the bad response
        return self._extract_partial_parse(bad_response)

    def _extract_partial_parse(self, text: str) -> dict | None:
        """Try to extract action intent from malformed response."""
        text_lower = text.lower().strip()
        if any(kw in text_lower for kw in ("final answer", "the answer is", "in conclusion")):
            return {
                "thought": "Extracted from malformed response",
                "action": "finish",
                "action_input": text.strip()[:2000],
            }
        return None

    async def _retry_parse(
        self,
        model: str,
        messages: list[dict],
        bad_response: str,
        temperature: float,
    ) -> dict | None:
        """Backward-compatible retry method."""
        return await self._retry_parse_enhanced(model, messages, bad_response, temperature)

    def _total_timeout_exceeded(self, start: float) -> bool:
        return (time.monotonic() - start) > self.total_timeout_sec

    def _extract_partial_answer(self, steps: list[AgentStep]) -> str:
        """Extract best answer from incomplete run."""
        if not steps:
            return "No steps completed."
        last = steps[-1]
        parts = []
        for s in steps:
            if s.observation and s.observation != "(finished)":
                parts.append(f"Step {s.step} ({s.action}): {s.observation[:200]}")
        if parts:
            return f"Partial results (max steps reached):\n" + "\n".join(parts[-3:])
        return f"Reached max steps. Last thought: {last.thought[:300]}"

    def _make_partial_result(
        self,
        model: str,
        steps: list[AgentStep],
        reason: str,
        tracer: TraceRecorder,
    ) -> AgentResult:
        answer = self._extract_partial_answer(steps) if steps else reason
        result = AgentResult(
            answer=answer, model=model, steps=steps, finished=False,
        )
        self._finalize_trace(tracer, result)
        return result

    def _finalize_trace(self, tracer: TraceRecorder, result: AgentResult) -> None:
        summary = tracer.summary()
        result.trace_summary = summary.to_dict()
        trace_path = tracer.save()
        if trace_path:
            logger.debug("Trace saved to %s", trace_path)
        if summary.total_steps > 0:
            logger.info(
                "Agent done: %d steps, %d+%d tokens, %.0fms, tools=%s",
                summary.total_steps, summary.total_tokens_in, summary.total_tokens_out,
                summary.total_duration_ms,
                list(summary.tool_stats.keys()) if summary.tool_stats else "none",
            )

    async def _maybe_compress_history(
        self,
        messages: list[dict],
        model: str,
    ) -> None:
        """Compress old messages if approaching context limit."""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        estimated_tokens = total_chars // 3  # rough chars-to-tokens

        try:
            ctx_len = await self.client.get_context_length(model)
            if not isinstance(ctx_len, (int, float)):
                ctx_len = 8192
        except Exception:
            ctx_len = 8192

        if estimated_tokens < int(ctx_len * CONTEXT_COMPRESS_RATIO):
            return

        self._compress_history(messages)

    def _compress_history(self, messages: list[dict]) -> None:
        """Summarize old conversation turns, keeping system + last 4 messages."""
        if len(messages) <= 5:
            return

        system_msg = messages[0] if messages[0]["role"] == "system" else None
        keep_start = 1 if system_msg else 0
        keep_tail = 4  # keep the most recent exchanges
        to_compress = messages[keep_start:-keep_tail]

        if not to_compress:
            return

        summary_parts = []
        for m in to_compress:
            role = m["role"]
            content = m.get("content", "")[:200]
            summary_parts.append(f"[{role}] {content}")

        summary = "Previous conversation summary:\n" + "\n".join(summary_parts)
        compressed_msg = {"role": "user", "content": summary}

        tail = messages[-keep_tail:]
        messages.clear()
        if system_msg:
            messages.append(system_msg)
        messages.append(compressed_msg)
        messages.extend(tail)
