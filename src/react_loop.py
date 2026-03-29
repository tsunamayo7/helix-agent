"""ReAct agent loop: Reason → Action → Observe → Reflect."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .ollama_client import OllamaClient
from .tools import ToolRegistry


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

    def to_dict(self) -> dict:
        return {
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


REACT_SYSTEM_PROMPT = """You are a helpful assistant that solves tasks step by step.
You have access to these tools:

{tools}

For each step, respond with ONLY this JSON (no markdown, no extra text):
{{"thought": "your reasoning about what to do next", "action": "tool_name", "action_input": "input for the tool"}}

When you have the final answer, use:
{{"thought": "I have the answer", "action": "finish", "action_input": "your final answer here"}}

Rules:
- Always think before acting
- Use tools when you need external information
- If a tool returns an error, try a different approach
- Be concise in your thoughts
- Respond with ONLY the JSON object, nothing else"""


class ReactLoop:
    """Execute a ReAct (Reason-Act) loop with a local LLM."""

    def __init__(
        self,
        client: OllamaClient,
        tools: ToolRegistry,
        max_steps: int = 10,
        max_retries: int = 2,
    ):
        self.client = client
        self.tools = tools
        self.max_steps = max_steps
        self.max_retries = max_retries

    async def run(
        self,
        task: str,
        model: str,
        *,
        context: str = "",
        temperature: float = 0.1,
        on_progress: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> AgentResult:
        """Run the ReAct loop until finish or max_steps.

        Args:
            on_progress: Optional async callback(step, max_steps, action) for progress reporting.
        """
        system = REACT_SYSTEM_PROMPT.format(tools=self.tools.format_for_prompt())

        messages = [
            {"role": "system", "content": system},
        ]

        user_content = task
        if context:
            user_content = f"{task}\n\nContext:\n{context}"
        messages.append({"role": "user", "content": user_content})

        steps: list[AgentStep] = []

        for step_num in range(1, self.max_steps + 1):
            # Get LLM response
            response = await self._call_llm(model, messages, temperature)

            # Parse the action
            parsed = self._parse_response(response)
            if parsed is None:
                # Retry with guidance
                retry_ok = await self._retry_parse(
                    model, messages, response, temperature
                )
                if retry_ok:
                    parsed = retry_ok
                else:
                    # Give up on this step, use raw response as answer
                    return AgentResult(
                        answer=response,
                        model=model,
                        steps=steps,
                        finished=False,
                    )

            thought = parsed.get("thought", "")
            action = parsed.get("action", "finish")
            action_input = parsed.get("action_input", "")

            step = AgentStep(
                step=step_num,
                thought=thought,
                action=action,
                action_input=str(action_input),
            )

            # Check for finish
            if action == "finish":
                step.observation = "(finished)"
                steps.append(step)
                return AgentResult(
                    answer=str(action_input),
                    model=model,
                    steps=steps,
                    finished=True,
                )

            # Report progress
            if on_progress:
                await on_progress(step_num, self.max_steps, action)

            # Execute tool
            observation = await self.tools.execute(action, str(action_input))
            step.observation = observation
            steps.append(step)

            # Add assistant response and observation to messages
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": f"Observation: {observation}\n\nContinue with the next step.",
            })

        # Max steps reached
        return AgentResult(
            answer=f"Reached max steps ({self.max_steps}). Last observation: {steps[-1].observation if steps else 'none'}",
            model=model,
            steps=steps,
            finished=False,
        )

    async def _call_llm(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
    ) -> str:
        """Call the LLM and return the response text."""
        return await self.client.chat(
            model=model,
            messages=messages,
            temperature=temperature,
            format_json=True,
        )

    def _parse_response(self, response: str) -> dict | None:
        """Parse the LLM response as JSON with thought/action/action_input."""
        text = response.strip()

        # Remove markdown code blocks if present
        if "```" in text:
            match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON object in the text
            match = re.search(r"\{[^{}]*\}", text)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        # Validate required fields
        if "action" not in data:
            return None

        return {
            "thought": data.get("thought", ""),
            "action": data["action"],
            "action_input": data.get("action_input", ""),
        }

    async def _retry_parse(
        self,
        model: str,
        messages: list[dict],
        bad_response: str,
        temperature: float,
    ) -> dict | None:
        """Retry when JSON parsing fails."""
        for _ in range(self.max_retries):
            retry_messages = messages + [
                {"role": "assistant", "content": bad_response},
                {
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. "
                        'Respond with ONLY a JSON object like: '
                        '{"thought": "...", "action": "tool_name", "action_input": "..."}'
                    ),
                },
            ]
            response = await self._call_llm(model, retry_messages, temperature)
            parsed = self._parse_response(response)
            if parsed:
                return parsed
        return None
