"""Self-evolving memory engine for helix-agent.

Inspired by NousResearch/hermes-agent's learning loop:
- Memory nudge: Every N turns, review conversation for saveable insights
- Skill auto-generation: Create reusable SKILL.md files from successful patterns
- Qdrant integration: Use existing mem0_shared for vector memory
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Optional


@dataclass
class EvolvingMemoryConfig:
    memory_nudge_interval: int = 5
    skill_nudge_interval: int = 8
    skills_dir: str = "~/.helix-agent/skills"
    review_model: str = ""  # auto-detect from GPU
    ollama_url: str = "http://localhost:11434"


class SkillStore:
    """File-based skill store (hermes-compatible SKILL.md format)."""

    def __init__(self, skills_dir: str):
        self.dir = Path(skills_dir).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[dict]:
        skills = []
        for skill_md in self.dir.rglob("SKILL.md"):
            content = skill_md.read_text(encoding="utf-8")
            skills.append({
                "name": skill_md.parent.name,
                "path": str(skill_md),
                "size": len(content),
            })
        return skills

    def get_skill(self, name: str) -> Optional[str]:
        skill_md = self.dir / name / "SKILL.md"
        if skill_md.exists():
            return skill_md.read_text(encoding="utf-8")
        return None

    def create_skill(self, name: str, description: str, content: str) -> str:
        skill_dir = self.dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        text = f"---\nname: {name}\ndescription: {description}\nversion: 1.0\ncreated: {datetime.now().isoformat()}\n---\n\n{content}"
        skill_md.write_text(text, encoding="utf-8")
        return str(skill_md)

    def patch_skill(self, name: str, old_text: str, new_text: str) -> bool:
        skill_md = self.dir / name / "SKILL.md"
        if not skill_md.exists():
            return False
        content = skill_md.read_text(encoding="utf-8")
        if old_text not in content:
            return False
        skill_md.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return True

    def delete_skill(self, name: str) -> bool:
        skill_dir = self.dir / name
        if skill_dir.exists():
            import shutil
            shutil.rmtree(skill_dir)
            return True
        return False


class EvolvingMemory:
    """Self-evolving memory engine.

    Uses local LLM (gemma4) for background review — $0 cost.
    Stores insights in Qdrant mem0_shared + file-based skills.
    """

    def __init__(self, config: EvolvingMemoryConfig | None = None):
        self.config = config or EvolvingMemoryConfig()
        if not self.config.review_model:
            from .gpu_detect import auto_select_model
            self.config.review_model = auto_select_model("review")
        self._skills = SkillStore(self.config.skills_dir)
        self._turn_count = 0
        self._tool_call_count = 0
        self._session_start = time.time()

    async def on_turn_end(
        self,
        user_message: str,
        assistant_response: str,
        tool_calls: list[dict] | None = None,
    ) -> dict:
        self._turn_count += 1
        self._tool_call_count += len(tool_calls or [])
        actions = {"memory_reviewed": False, "skill_action": None}

        if self._turn_count % self.config.memory_nudge_interval == 0:
            review = await self._review_for_memory(user_message, assistant_response)
            if review.get("should_save"):
                actions["memory_reviewed"] = True
                actions["memory_content"] = review.get("content", "")
                actions["memory_type"] = review.get("type", "observation")

        if self._tool_call_count >= self.config.skill_nudge_interval:
            self._tool_call_count = 0
            review = await self._review_for_skill(user_message, assistant_response, tool_calls)
            if review.get("action") in ("create", "patch"):
                actions["skill_action"] = review

        return actions

    async def _ollama_chat(self, prompt: str) -> str:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.config.ollama_url}/api/generate",
                    json={
                        "model": self.config.review_model,
                        "prompt": prompt,
                        "stream": False,
                        "format": "json",
                    },
                )
                if resp.status_code == 200:
                    return resp.json().get("response", "{}")
        except Exception:
            pass
        return "{}"

    async def _review_for_memory(self, user_msg: str, asst_msg: str) -> dict:
        prompt = (
            "Analyze this conversation turn and decide if anything should be saved as a persistent memory.\n\n"
            f"User: {user_msg[:500]}\n"
            f"Assistant: {asst_msg[:500]}\n\n"
            "Criteria: user preferences, corrections, environment facts, repeated patterns.\n"
            'JSON: {"should_save": bool, "content": "what to save", "type": "preference|correction|fact"}\n'
            'If nothing to save: {"should_save": false}'
        )
        result = await self._ollama_chat(prompt)
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return {"should_save": False}

    async def _review_for_skill(self, user_msg, asst_msg, tool_calls) -> dict:
        existing = [s["name"] for s in self._skills.list_skills()]
        prompt = (
            "Analyze if this task completion should be saved as a reusable skill.\n\n"
            f"Existing skills: {existing}\n"
            f"Tool calls: {len(tool_calls or [])}\n"
            f"User: {user_msg[:300]}\n"
            f"Assistant: {asst_msg[:300]}\n\n"
            'JSON for new skill: {"action": "create", "name": "skill-name", "description": "...", "content": "steps..."}\n'
            'JSON for update: {"action": "patch", "name": "existing", "old_text": "...", "new_text": "..."}\n'
            'JSON if not needed: {"action": "none"}'
        )
        result = await self._ollama_chat(prompt)
        try:
            parsed = json.loads(result)
            if parsed.get("action") == "create" and parsed.get("name") and parsed.get("content"):
                self._skills.create_skill(parsed["name"], parsed.get("description", ""), parsed["content"])
            elif parsed.get("action") == "patch" and parsed.get("name"):
                self._skills.patch_skill(parsed["name"], parsed.get("old_text", ""), parsed.get("new_text", ""))
            return parsed
        except json.JSONDecodeError:
            return {"action": "none"}

    def get_skills_context(self) -> str:
        skills = self._skills.list_skills()
        if not skills:
            return ""
        return "Available learned skills:\n" + "\n".join(f"- {s['name']}" for s in skills)

    def stats(self) -> dict:
        return {
            "turns": self._turn_count,
            "tool_calls": self._tool_call_count,
            "skills_count": len(self._skills.list_skills()),
            "uptime_seconds": int(time.time() - self._session_start),
        }
