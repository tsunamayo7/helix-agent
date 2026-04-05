"""Tests for token_saver: vision_compress, dom_compress, retry_guard."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from src.token_saver import (
    RetryGuard,
    TokenSaver,
    _estimate_tokens,
    _try_parse_json,
)


# ── _try_parse_json ──

def test_try_parse_json_plain():
    assert _try_parse_json('{"a": 1}') == {"a": 1}


def test_try_parse_json_with_fences():
    text = '```json\n{"a": 1, "b": "x"}\n```'
    assert _try_parse_json(text) == {"a": 1, "b": "x"}


def test_try_parse_json_embedded_in_prose():
    text = 'Here is the result:\n{"role": "button", "label": "Login"}\nEnd.'
    assert _try_parse_json(text) == {"role": "button", "label": "Login"}


def test_try_parse_json_empty_returns_none():
    assert _try_parse_json("") is None


def test_try_parse_json_invalid_returns_none():
    assert _try_parse_json("this is not json") is None


def test_try_parse_json_malformed_returns_none():
    assert _try_parse_json("{broken: json}") is None


# ── _estimate_tokens ──

def test_estimate_tokens_scales_with_length():
    short = _estimate_tokens("hello")
    long = _estimate_tokens("hello " * 1000)
    assert short >= 1
    assert long > short
    assert long > 1000  # 6000 chars / 4 ≈ 1500


def test_estimate_tokens_empty_returns_min_one():
    assert _estimate_tokens("") == 1


# ── RetryGuard ──

def test_retry_guard_first_call_no_loop():
    guard = RetryGuard(threshold=3)
    result = guard.check("read_file", {"path": "a.py"})
    assert result["loop_detected"] is False
    assert result["repeat_count"] == 1


def test_retry_guard_detects_loop_at_threshold():
    guard = RetryGuard(threshold=3)
    guard.check("read_file", {"path": "a.py"})
    guard.check("read_file", {"path": "a.py"})
    result = guard.check("read_file", {"path": "a.py"})
    assert result["loop_detected"] is True
    assert result["repeat_count"] == 3
    assert "retry loop" in result["recommendation"]


def test_retry_guard_different_args_no_loop():
    guard = RetryGuard(threshold=3)
    guard.check("read_file", {"path": "a.py"})
    guard.check("read_file", {"path": "b.py"})
    result = guard.check("read_file", {"path": "c.py"})
    assert result["loop_detected"] is False
    assert result["repeat_count"] == 1


def test_retry_guard_warning_one_before_threshold():
    guard = RetryGuard(threshold=3)
    guard.check("grep", {"pattern": "TODO"})
    result = guard.check("grep", {"pattern": "TODO"})
    assert result["loop_detected"] is False
    assert result["repeat_count"] == 2
    assert "One more repeat" in result["recommendation"]


def test_retry_guard_reset_clears_session():
    guard = RetryGuard(threshold=3)
    guard.check("x", {"a": 1})
    guard.check("x", {"a": 1})
    result = guard.reset()
    assert result["cleared_entries"] == 2
    # Next call should be fresh
    fresh = guard.check("x", {"a": 1})
    assert fresh["repeat_count"] == 1


def test_retry_guard_session_isolation():
    guard = RetryGuard(threshold=3)
    guard.check("x", {"a": 1}, session_id="session_a")
    guard.check("x", {"a": 1}, session_id="session_a")
    result = guard.check("x", {"a": 1}, session_id="session_b")
    assert result["loop_detected"] is False
    assert result["repeat_count"] == 1


def test_retry_guard_window_prunes_old_entries():
    guard = RetryGuard(threshold=3, window_seconds=1)
    guard.check("x", {"a": 1})
    guard.check("x", {"a": 1})
    time.sleep(1.2)
    # Prior entries expired
    result = guard.check("x", {"a": 1})
    assert result["repeat_count"] == 1


def test_retry_guard_status_reports_stats():
    guard = RetryGuard(threshold=3)
    guard.check("read", {"p": "a"})
    guard.check("read", {"p": "a"})
    guard.check("write", {"p": "b"})
    s = guard.status()
    assert s["total_calls"] == 3
    assert s["unique_calls"] == 2
    assert s["max_repeats"] == 2


def test_retry_guard_custom_threshold():
    guard = RetryGuard(threshold=5)
    for _ in range(4):
        r = guard.check("x", {"a": 1})
        assert r["loop_detected"] is False
    result = guard.check("x", {"a": 1})
    assert result["loop_detected"] is True
    assert result["repeat_count"] == 5


def test_retry_guard_hash_stability():
    """Same args in different order should still match."""
    guard = RetryGuard(threshold=2)
    guard.check("tool", {"a": 1, "b": 2})
    result = guard.check("tool", {"b": 2, "a": 1})
    # sort_keys=True in hash ensures these are treated as identical
    assert result["repeat_count"] == 2
    assert result["loop_detected"] is True


# ── TokenSaver.vision_compress ──

@pytest.mark.asyncio
async def test_vision_compress_missing_inputs():
    ts = TokenSaver()
    result = await ts.vision_compress()
    assert "error" in result


@pytest.mark.asyncio
async def test_vision_compress_nonexistent_file():
    ts = TokenSaver()
    result = await ts.vision_compress(image_path="/nonexistent/path.png")
    assert "error" in result


@pytest.mark.asyncio
async def test_vision_compress_parses_structured_response():
    ts = TokenSaver()
    fake_response = json.dumps({
        "page_type": "login",
        "title": "Sign in",
        "primary_action": "click login button",
        "interactive_elements": [],
        "key_text": ["Username", "Password"],
        "state_flags": {"has_error": False, "has_modal": False,
                        "requires_auth": True, "loading": False},
        "notes": "standard login form"
    })
    with patch.object(ts.vision, "analyze", new=AsyncMock(return_value=fake_response)):
        result = await ts.vision_compress(image_base64="fake_b64_data")
    assert "error" not in result
    assert result["summary"]["page_type"] == "login"
    assert result["summary"]["state_flags"]["requires_auth"] is True
    assert result["tokens_saved_estimate"] >= 0


@pytest.mark.asyncio
async def test_vision_compress_unparsable_response_preserved():
    ts = TokenSaver()
    with patch.object(ts.vision, "analyze", new=AsyncMock(return_value="This is not JSON at all")):
        result = await ts.vision_compress(image_base64="fake_b64_data")
    assert "_unparsed" in result["summary"]


@pytest.mark.asyncio
async def test_vision_compress_custom_prompt():
    ts = TokenSaver()
    captured = {}

    async def fake_analyze(img, prompt, model=None):
        captured["prompt"] = prompt
        return '{"ok": true}'

    with patch.object(ts.vision, "analyze", new=fake_analyze):
        await ts.vision_compress(image_base64="x", custom_prompt="EXTRACT BUTTONS ONLY")
    assert "EXTRACT BUTTONS ONLY" in captured["prompt"]


# ── TokenSaver.dom_compress ──

@pytest.mark.asyncio
async def test_dom_compress_missing_inputs():
    ts = TokenSaver()
    result = await ts.dom_compress()
    assert "error" in result


@pytest.mark.asyncio
async def test_dom_compress_returns_structure():
    ts = TokenSaver()
    fake_summary = json.dumps({
        "url": "https://example.com",
        "title": "Example",
        "summary": "test page",
        "forms": [],
        "links": [{"text": "Home", "href": "/"}],
        "buttons": [],
        "main_content": "hello world",
        "errors": [],
        "next_action_candidates": ["read content"]
    })
    with patch("src.token_saver._ollama_generate", new=AsyncMock(return_value=fake_summary)):
        result = await ts.dom_compress(
            html="<html><body><h1>Example</h1><a href='/'>Home</a></body></html>",
            url="https://example.com"
        )
    assert "error" not in result
    assert result["summary"]["title"] == "Example"
    assert result["summary"]["links"][0]["text"] == "Home"
    assert result["original_char_count"] > 0


@pytest.mark.asyncio
async def test_dom_compress_truncates_large_payload():
    ts = TokenSaver()
    big_html = "<p>content</p>" * 5000  # ~70,000 chars
    captured = {}

    async def fake_generate(host, model, prompt, timeout):
        captured["prompt_length"] = len(prompt)
        return json.dumps({"url": "", "title": "", "summary": "big",
                           "forms": [], "links": [], "buttons": [],
                           "main_content": "", "errors": [], "next_action_candidates": []})

    with patch("src.token_saver._ollama_generate", new=fake_generate):
        result = await ts.dom_compress(html=big_html, url="https://big.example")
    # Prompt should have been truncated to keep local LLM fast
    assert captured["prompt_length"] < 32000
    assert result["original_char_count"] == len(big_html)


@pytest.mark.asyncio
async def test_dom_compress_estimates_savings():
    ts = TokenSaver()
    large_content = "a" * 20000  # ~5000 tokens
    with patch("src.token_saver._ollama_generate",
               new=AsyncMock(return_value='{"url":"","title":"","summary":"","forms":[],"links":[],"buttons":[],"main_content":"","errors":[],"next_action_candidates":[]}')):
        result = await ts.dom_compress(text_content=large_content)
    # Should estimate at least some token savings
    assert result["tokens_saved_estimate"] > 1000


@pytest.mark.asyncio
async def test_dom_compress_text_content_alternative():
    ts = TokenSaver()
    with patch("src.token_saver._ollama_generate",
               new=AsyncMock(return_value='{"url":"u","title":"t","summary":"s","forms":[],"links":[],"buttons":[],"main_content":"","errors":[],"next_action_candidates":[]}')):
        result = await ts.dom_compress(text_content="Just some plain text", url="u")
    assert result["summary"]["title"] == "t"
