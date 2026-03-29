"""Tests for the benchmark engine."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.benchmark import (
    BENCHMARK_LITE_TESTS,
    BenchmarkEngine,
    BenchmarkTest,
    ModelBenchmark,
    _adaptive_timeout,
    preflight_check,
    validate_code_fizzbuzz,
    validate_code_reverse,
    validate_exact_number,
    validate_json_output,
    validate_japanese_summary,
    validate_japanese_text,
    validate_numbered_list,
    validate_reasoning_logic,
)


# --- Validator tests ---


class TestValidateCodeFizzbuzz:
    def test_valid_fizzbuzz(self):
        code = '''def fizzbuzz(n):
    result = []
    for i in range(1, n + 1):
        if i % 3 == 0 and i % 5 == 0:
            result.append("FizzBuzz")
        elif i % 3 == 0:
            result.append("Fizz")
        elif i % 5 == 0:
            result.append("Buzz")
        else:
            result.append(str(i))
    return result'''
        passed, score = validate_code_fizzbuzz(code)
        assert passed
        assert score >= 0.75

    def test_code_in_markdown_block(self):
        text = '```python\ndef fizzbuzz(n):\n    return ["FizzBuzz" if i%3==0 and i%5==0 else "Fizz" if i%3==0 else "Buzz" if i%5==0 else str(i) for i in range(1,n+1)]\n```'
        passed, score = validate_code_fizzbuzz(text)
        assert passed

    def test_no_fizzbuzz(self):
        passed, score = validate_code_fizzbuzz("Hello world")
        assert not passed
        assert score == 0.0


class TestValidateCodeReverse:
    def test_valid_reverse(self):
        code = 'def reverse_words(s):\n    return " ".join(s.split()[::-1])'
        passed, score = validate_code_reverse(code)
        assert passed
        assert score >= 0.6

    def test_invalid(self):
        passed, score = validate_code_reverse("just some text")
        assert not passed


class TestValidateReasoningLogic:
    def test_correct_no(self):
        passed, score = validate_reasoning_logic("No, we cannot conclude that.")
        assert passed
        assert score >= 0.9

    def test_cannot_conclude(self):
        passed, score = validate_reasoning_logic("We cannot conclude that some roses fade quickly.")
        assert passed
        assert score >= 0.8

    def test_incorrect_yes(self):
        passed, score = validate_reasoning_logic("Yes, some roses must fade quickly.")
        assert not passed


class TestValidateExactNumber:
    def test_correct(self):
        passed, score = validate_exact_number("424", "424")
        assert passed
        assert score == 1.0

    def test_with_text(self):
        passed, score = validate_exact_number("The answer is 424.", "424")
        assert passed

    def test_wrong(self):
        passed, score = validate_exact_number("425", "424")
        assert not passed


class TestValidateJsonOutput:
    def test_valid_json(self):
        text = '{"name": "Alice", "age": 30, "city": "Tokyo"}'
        passed, score = validate_json_output(text)
        assert passed
        assert score >= 0.9

    def test_json_in_code_block(self):
        text = '```json\n{"name": "Alice", "age": 30, "city": "Tokyo"}\n```'
        passed, score = validate_json_output(text)
        assert passed

    def test_invalid_json(self):
        passed, score = validate_json_output("not json at all")
        assert not passed


class TestValidateNumberedList:
    def test_three_items(self):
        text = "1. Python\n2. JavaScript\n3. Rust"
        passed, score = validate_numbered_list(text)
        assert passed
        assert score == 1.0

    def test_two_items(self):
        text = "1. Python\n2. JavaScript"
        passed, score = validate_numbered_list(text)
        assert passed
        assert score == 0.7


class TestValidateJapaneseText:
    def test_japanese(self):
        passed, score = validate_japanese_text("今日は天気がいいので散歩に行きましょう。")
        assert passed
        assert score >= 0.5

    def test_no_japanese(self):
        passed, score = validate_japanese_text("Hello, the weather is nice today.")
        assert not passed


class TestValidateJapaneseSummary:
    def test_valid_summary(self):
        text = "AI技術の発展により製造業や医療など多くの産業で自動化が進んでいる。"
        passed, score = validate_japanese_summary(text)
        assert passed
        assert score >= 0.5

    def test_no_japanese(self):
        passed, score = validate_japanese_summary("AI is advancing.")
        assert not passed


# --- ModelBenchmark serialization ---


class TestModelBenchmark:
    def test_round_trip(self):
        bm = ModelBenchmark(
            model_name="test:7b",
            timestamp="2026-01-01T00:00:00Z",
            total_score=75.0,
            category_scores={"code": 80.0, "reasoning": 70.0},
            avg_tokens_per_sec=15.0,
            results=[{"test": "fizzbuzz", "score": 0.8}],
        )
        d = bm.to_dict()
        restored = ModelBenchmark.from_dict(d)
        assert restored.model_name == "test:7b"
        assert restored.total_score == 75.0
        assert restored.category_scores["code"] == 80.0


# --- BenchmarkEngine ---


class TestBenchmarkEngine:
    def test_cache_save_load(self, tmp_path):
        cache_path = tmp_path / "bench.json"
        client = AsyncMock()
        engine = BenchmarkEngine(client, cache_path=cache_path)

        bm = ModelBenchmark(
            model_name="test:7b",
            total_score=80.0,
            category_scores={"code": 90.0},
        )
        engine._cache["test:7b"] = bm
        engine._save_cache()

        # Reload
        engine2 = BenchmarkEngine(client, cache_path=cache_path)
        assert "test:7b" in engine2._cache
        assert engine2._cache["test:7b"].total_score == 80.0

    def test_get_unbenchmarked(self, tmp_path):
        cache_path = tmp_path / "bench.json"
        client = AsyncMock()
        engine = BenchmarkEngine(client, cache_path=cache_path)
        engine._cache["model-a"] = ModelBenchmark(model_name="model-a")

        unbenched = engine.get_unbenchmarked(["model-a", "model-b", "model-c"])
        assert unbenched == ["model-b", "model-c"]

    def test_remove_cached(self, tmp_path):
        cache_path = tmp_path / "bench.json"
        client = AsyncMock()
        engine = BenchmarkEngine(client, cache_path=cache_path)
        engine._cache["test:7b"] = ModelBenchmark(model_name="test:7b")
        engine._save_cache()

        assert engine.remove_cached("test:7b")
        assert "test:7b" not in engine._cache
        assert not engine.remove_cached("nonexistent")

    @pytest.mark.asyncio
    async def test_run_benchmark(self, tmp_path):
        cache_path = tmp_path / "bench.json"
        client = AsyncMock()
        client.timeout = 120.0
        # Mock chat: warmup + 8 tests = 9 calls
        responses = iter([
            "ok",  # warmup
            'def fizzbuzz(n):\n    return ["FizzBuzz" if i%3==0 and i%5==0 else "Fizz" if i%3==0 else "Buzz" if i%5==0 else str(i) for i in range(1,n+1)]',
            'def reverse_words(s):\n    return " ".join(s.split()[::-1])',
            "No, we cannot conclude that.",
            "424",
            '{"name": "Alice", "age": 30, "city": "Tokyo"}',
            "1. Python\n2. JavaScript\n3. Rust",
            "今日は天気がいいので、散歩に行きましょう。",
            "AI技術の発展により多くの産業で自動化が進んでいる。",
        ])
        client.chat = AsyncMock(side_effect=lambda **kwargs: next(responses))

        engine = BenchmarkEngine(client, cache_path=cache_path)
        result = await engine.run_benchmark("test:7b")

        assert result.model_name == "test:7b"
        assert result.total_score > 0
        assert len(result.results) == 8
        assert "test:7b" in engine._cache

    @pytest.mark.asyncio
    async def test_run_benchmark_lite(self, tmp_path):
        cache_path = tmp_path / "bench.json"
        client = AsyncMock()
        client.timeout = 120.0
        responses = iter([
            "ok",  # warmup
            'def fizzbuzz(n):\n    return ["FizzBuzz" if i%3==0 and i%5==0 else "Fizz" if i%3==0 else "Buzz" if i%5==0 else str(i) for i in range(1,n+1)]',
            "424",
            '{"name": "Alice", "age": 30, "city": "Tokyo"}',
        ])
        client.chat = AsyncMock(side_effect=lambda **kwargs: next(responses))

        engine = BenchmarkEngine(client, cache_path=cache_path)
        result = await engine.run_benchmark("big:122b", lite=True)

        assert result.model_name == "big:122b"
        assert len(result.results) == 3  # lite = 3 tests

    @pytest.mark.asyncio
    async def test_run_benchmark_warmup_failure(self, tmp_path):
        cache_path = tmp_path / "bench.json"
        client = AsyncMock()
        client.timeout = 120.0
        client.chat = AsyncMock(side_effect=Exception("500 Internal Server Error"))

        engine = BenchmarkEngine(client, cache_path=cache_path)
        result = await engine.run_benchmark("broken:70b")

        assert result.model_name == "broken:70b"
        assert result.total_score == 0.0
        assert result.results[0].get("warmup_failed") is True

    @pytest.mark.asyncio
    async def test_warmup_success(self, tmp_path):
        cache_path = tmp_path / "bench.json"
        client = AsyncMock()
        client.timeout = 120.0
        client.chat = AsyncMock(return_value="ok")

        engine = BenchmarkEngine(client, cache_path=cache_path)
        result = await engine.warmup("test:7b")

        assert result["success"] is True
        assert result["load_time_sec"] >= 0


# --- Adaptive timeout and preflight ---


class TestAdaptiveTimeout:
    def test_small_model(self):
        assert _adaptive_timeout(5.0) == 30.0

    def test_medium_model(self):
        assert _adaptive_timeout(15.0) == 60.0

    def test_large_model(self):
        assert _adaptive_timeout(50.0) == 120.0

    def test_huge_model(self):
        assert _adaptive_timeout(80.0) == 180.0


class TestPreflightCheck:
    def test_sufficient_vram(self):
        gpus = [{"name": "RTX PRO 6000", "vram_total_mb": 98304, "vram_free_mb": 90000}]
        result = preflight_check(70.0, gpus=gpus)
        assert result["can_run"] is True

    def test_insufficient_vram(self):
        gpus = [{"name": "RTX 4060", "vram_total_mb": 8192, "vram_free_mb": 6000}]
        result = preflight_check(70.0, gpus=gpus)
        assert result["can_run"] is False

    def test_partial_offload(self):
        gpus = [{"name": "RTX 5070 Ti", "vram_total_mb": 16384, "vram_free_mb": 14000}]
        result = preflight_check(20.0, gpus=gpus)
        assert result["can_run"] is True

    def test_no_gpu_info(self):
        result = preflight_check(50.0, gpus=[])
        assert result["can_run"] is True  # Assume yes if GPU unknown

    def test_multi_gpu(self):
        gpus = [
            {"name": "RTX 5070 Ti", "vram_total_mb": 16384, "vram_free_mb": 14000},
            {"name": "RTX PRO 6000", "vram_total_mb": 98304, "vram_free_mb": 90000},
        ]
        result = preflight_check(80.0, gpus=gpus)
        assert result["can_run"] is True
        assert result["total_free_gb"] > 100


class TestBenchmarkLiteTests:
    def test_lite_has_3_tests(self):
        assert len(BENCHMARK_LITE_TESTS) == 3

    def test_lite_test_names(self):
        names = [t.name for t in BENCHMARK_LITE_TESTS]
        assert "fizzbuzz" in names
        assert "math" in names
        assert "json_output" in names


# --- Router with benchmark integration ---


class TestRouterBenchmarkIntegration:
    def test_model_override(self):
        from src.router import ModelRouter

        client = AsyncMock()
        router = ModelRouter(client)

        assert router.get_model_override() is None
        router.set_model_override("custom:7b")
        assert router.get_model_override() == "custom:7b"
        router.set_model_override(None)
        assert router.get_model_override() is None

    @pytest.mark.asyncio
    async def test_select_respects_override(self):
        from src.router import Capability, ModelRouter

        client = AsyncMock()
        client.list_models = AsyncMock(return_value=[
            {"name": "gemma3:4b", "size": 3 * 1024**3, "details": {"family": "gemma", "parameter_size": "4B", "quantization_level": "Q4_K_M"}},
        ])

        router = ModelRouter(client)
        router.set_model_override("my-custom-model:latest")

        selected = await router.select(Capability.CODE)
        assert selected == "my-custom-model:latest"

    @pytest.mark.asyncio
    async def test_select_for_task_respects_override(self):
        from src.router import ModelRouter

        client = AsyncMock()
        router = ModelRouter(client)
        router.set_model_override("forced:13b")

        selected = await router.select_for_task("Write a function")
        assert selected == "forced:13b"
