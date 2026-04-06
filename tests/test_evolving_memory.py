"""Tests for evolving memory system."""

import json
import tempfile
from pathlib import Path

import pytest

from src.evolving_memory import EvolvingMemory, EvolvingMemoryConfig, SkillStore


class TestSkillStore:
    def test_create_and_list(self, tmp_path):
        store = SkillStore(str(tmp_path / "skills"))
        path = store.create_skill("test-skill", "A test skill", "Step 1: do thing")
        assert Path(path).exists()
        skills = store.list_skills()
        assert len(skills) == 1
        assert skills[0]["name"] == "test-skill"

    def test_get_skill(self, tmp_path):
        store = SkillStore(str(tmp_path / "skills"))
        store.create_skill("my-skill", "desc", "content here")
        content = store.get_skill("my-skill")
        assert "content here" in content
        assert store.get_skill("nonexistent") is None

    def test_patch_skill(self, tmp_path):
        store = SkillStore(str(tmp_path / "skills"))
        store.create_skill("patch-me", "desc", "old content")
        assert store.patch_skill("patch-me", "old content", "new content")
        content = store.get_skill("patch-me")
        assert "new content" in content
        assert not store.patch_skill("patch-me", "nonexistent text", "x")

    def test_delete_skill(self, tmp_path):
        store = SkillStore(str(tmp_path / "skills"))
        store.create_skill("to-delete", "desc", "content")
        assert store.delete_skill("to-delete")
        assert len(store.list_skills()) == 0
        assert not store.delete_skill("nonexistent")


class TestEvolvingMemory:
    def test_stats_initial(self):
        config = EvolvingMemoryConfig(skills_dir=tempfile.mkdtemp())
        mem = EvolvingMemory(config)
        stats = mem.stats()
        assert stats["turns"] == 0
        assert stats["tool_calls"] == 0
        assert stats["skills_count"] == 0

    def test_get_skills_context_empty(self):
        config = EvolvingMemoryConfig(skills_dir=tempfile.mkdtemp())
        mem = EvolvingMemory(config)
        assert mem.get_skills_context() == ""

    def test_get_skills_context_with_skills(self):
        td = tempfile.mkdtemp()
        config = EvolvingMemoryConfig(skills_dir=td)
        mem = EvolvingMemory(config)
        mem._skills.create_skill("test", "desc", "content")
        ctx = mem.get_skills_context()
        assert "test" in ctx

    @pytest.mark.asyncio
    async def test_on_turn_end_no_nudge(self):
        config = EvolvingMemoryConfig(
            skills_dir=tempfile.mkdtemp(),
            memory_nudge_interval=10,
            skill_nudge_interval=10,
        )
        mem = EvolvingMemory(config)
        result = await mem.on_turn_end("hello", "hi there", [])
        assert result["memory_reviewed"] is False
        assert result["skill_action"] is None
