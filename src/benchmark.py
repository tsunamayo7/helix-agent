"""Local benchmark engine: evaluate Ollama models on the user's hardware."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ollama_client import OllamaClient

# Default cache location
DEFAULT_CACHE_PATH = Path.home() / ".helix-agent" / "benchmarks.json"


@dataclass
class BenchmarkResult:
    """Result of a single benchmark test."""

    category: str
    test_name: str
    passed: bool
    score: float  # 0.0 - 1.0
    response_time_sec: float
    tokens_per_sec: float = 0.0


@dataclass
class ModelBenchmark:
    """Aggregated benchmark results for a model."""

    model_name: str
    timestamp: str = ""
    total_score: float = 0.0  # weighted aggregate 0-100
    results: list[dict] = field(default_factory=list)
    category_scores: dict[str, float] = field(default_factory=dict)
    avg_tokens_per_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "timestamp": self.timestamp,
            "total_score": self.total_score,
            "category_scores": self.category_scores,
            "avg_tokens_per_sec": self.avg_tokens_per_sec,
            "results": self.results,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ModelBenchmark:
        return cls(
            model_name=data["model_name"],
            timestamp=data.get("timestamp", ""),
            total_score=data.get("total_score", 0.0),
            category_scores=data.get("category_scores", {}),
            avg_tokens_per_sec=data.get("avg_tokens_per_sec", 0.0),
            results=data.get("results", []),
        )


# --- Benchmark test definitions ---

@dataclass
class BenchmarkTest:
    """A single benchmark test case."""

    category: str
    name: str
    prompt: str
    validator: str  # validator function name
    expected: str = ""  # expected answer or pattern
    weight: float = 1.0


# Category weights for final score calculation
CATEGORY_WEIGHTS: dict[str, float] = {
    "code": 0.25,
    "reasoning": 0.25,
    "instruction_following": 0.20,
    "japanese": 0.15,
    "speed": 0.15,
}

BENCHMARK_TESTS: list[BenchmarkTest] = [
    # --- Code generation ---
    BenchmarkTest(
        category="code",
        name="fizzbuzz",
        prompt=(
            "Write a Python function called fizzbuzz(n) that returns a list of strings from 1 to n. "
            "For multiples of 3 use 'Fizz', multiples of 5 use 'Buzz', multiples of both use 'FizzBuzz', "
            "otherwise the number as string. Output ONLY the function code, no explanation."
        ),
        validator="validate_code_fizzbuzz",
    ),
    BenchmarkTest(
        category="code",
        name="reverse_string",
        prompt=(
            "Write a Python function called reverse_words(s) that reverses the order of words in a string. "
            "Example: reverse_words('hello world') returns 'world hello'. "
            "Output ONLY the function code, no explanation."
        ),
        validator="validate_code_reverse",
    ),
    # --- Reasoning ---
    BenchmarkTest(
        category="reasoning",
        name="logic_puzzle",
        prompt=(
            "If all roses are flowers, and some flowers fade quickly, "
            "can we conclude that some roses fade quickly? "
            "Answer with ONLY 'Yes' or 'No' and one sentence of explanation."
        ),
        validator="validate_reasoning_logic",
        expected="No",
    ),
    BenchmarkTest(
        category="reasoning",
        name="math",
        prompt="What is 17 * 23 + 45 - 12? Answer with ONLY the number.",
        validator="validate_exact_number",
        expected="424",
    ),
    # --- Instruction following ---
    BenchmarkTest(
        category="instruction_following",
        name="json_output",
        prompt=(
            'Output a JSON object with exactly these keys: "name", "age", "city". '
            'Use values: name="Alice", age=30, city="Tokyo". '
            "Output ONLY the JSON, nothing else."
        ),
        validator="validate_json_output",
    ),
    BenchmarkTest(
        category="instruction_following",
        name="list_format",
        prompt=(
            "List exactly 3 programming languages. "
            "Output as a numbered list (1. 2. 3.) with no other text."
        ),
        validator="validate_numbered_list",
    ),
    # --- Japanese ---
    BenchmarkTest(
        category="japanese",
        name="translation",
        prompt=(
            "Translate to Japanese: 'The weather is nice today, let's go for a walk.' "
            "Output ONLY the Japanese translation."
        ),
        validator="validate_japanese_text",
    ),
    BenchmarkTest(
        category="japanese",
        name="summarize",
        prompt=(
            "以下の文を一文で要約してください: "
            "「人工知能技術の発展により、多くの産業で自動化が進んでいる。"
            "製造業ではロボットが組立作業を行い、"
            "医療では画像診断の精度が向上している。」"
            "出力は要約文のみ。"
        ),
        validator="validate_japanese_summary",
    ),
]


# --- Validators ---

def validate_code_fizzbuzz(response: str) -> tuple[bool, float]:
    """Check if the response contains a valid fizzbuzz implementation."""
    resp_lower = response.lower()
    code = _extract_code(response) if "```" in response else response

    # Check for function definition
    has_def = "def fizzbuzz" in resp_lower or "def fizzbuzz" in code.lower()
    if not has_def:
        return False, 0.0

    # Check key patterns
    score = 0.0
    check_text = code.lower() if code != response else resp_lower
    if "fizzbuzz" in check_text:
        score += 0.25
    if "fizz" in check_text and "buzz" in check_text:
        score += 0.25
    if re.search(r"%\s*3|mod.*3|divisible.*3", check_text):
        score += 0.25
    if re.search(r"%\s*5|mod.*5|divisible.*5", check_text):
        score += 0.25
    return score >= 0.75, score


def validate_code_reverse(response: str) -> tuple[bool, float]:
    """Check if response contains a valid reverse_words function."""
    resp_lower = response.lower().replace(" ", "")
    if "defreverse_words" not in resp_lower and "def reverse_words" not in response:
        code = _extract_code(response)
        if "def reverse_words" not in code:
            return False, 0.0

    score = 0.0
    if "split" in response:
        score += 0.4
    if "reverse" in response or "[::-1]" in response or "reversed" in response:
        score += 0.4
    if "join" in response:
        score += 0.2
    return score >= 0.6, score


def validate_reasoning_logic(response: str) -> tuple[bool, float]:
    """The correct answer is 'No' — this is a classic syllogism fallacy."""
    resp_lower = response.lower().strip()
    # Check if answer starts with or contains "no"
    if resp_lower.startswith("no"):
        return True, 1.0
    if re.search(r"\bno\b", resp_lower[:50]):
        return True, 0.9
    # "cannot conclude" also acceptable
    if "cannot" in resp_lower or "can't" in resp_lower or "not necessarily" in resp_lower:
        return True, 0.8
    return False, 0.0


def validate_exact_number(response: str, expected: str = "424") -> tuple[bool, float]:
    """Check if response contains the exact expected number."""
    numbers = re.findall(r"\b\d+\b", response)
    if expected in numbers:
        return True, 1.0
    # Partial credit if the number appears anywhere
    if expected in response:
        return True, 0.9
    return False, 0.0


def validate_json_output(response: str) -> tuple[bool, float]:
    """Check if response is valid JSON with expected keys."""
    # Try to extract JSON from response
    text = response.strip()
    # Remove markdown code block if present
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON-like content
        match = re.search(r"\{[^}]+\}", text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return False, 0.0
        else:
            return False, 0.0

    score = 0.0
    if isinstance(data, dict):
        score += 0.2
        if "name" in data:
            score += 0.2
            if data["name"] == "Alice":
                score += 0.1
        if "age" in data:
            score += 0.2
            if data["age"] == 30:
                score += 0.1
        if "city" in data:
            score += 0.1
            if data["city"] == "Tokyo":
                score += 0.1
    return score >= 0.6, score


def validate_numbered_list(response: str) -> tuple[bool, float]:
    """Check if response is a numbered list with exactly 3 items."""
    lines = [l.strip() for l in response.strip().split("\n") if l.strip()]
    numbered = [l for l in lines if re.match(r"^\d+[\.\)]\s*\w+", l)]

    if len(numbered) == 3:
        return True, 1.0
    if len(numbered) >= 2:
        return True, 0.7
    if len(numbered) == 1:
        return False, 0.3
    return False, 0.0


def validate_japanese_text(response: str) -> tuple[bool, float]:
    """Check if response contains Japanese characters."""
    # Count Japanese characters (hiragana, katakana, kanji)
    jp_chars = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", response))
    if jp_chars >= 5:
        return True, min(1.0, jp_chars / 10)
    if jp_chars >= 1:
        return True, 0.3
    return False, 0.0


def validate_japanese_summary(response: str) -> tuple[bool, float]:
    """Check if response is a Japanese summary."""
    jp_chars = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", response))
    if jp_chars < 3:
        return False, 0.0

    score = 0.3  # Has Japanese
    # Should be concise (one sentence)
    if len(response.strip().split("\n")) <= 2:
        score += 0.3
    # Should mention key topics
    if any(kw in response for kw in ["AI", "人工知能", "自動化", "技術"]):
        score += 0.2
    if any(kw in response for kw in ["製造", "医療", "産業"]):
        score += 0.2
    return score >= 0.5, min(1.0, score)


def _extract_code(text: str) -> str:
    """Extract code from markdown code blocks."""
    match = re.search(r"```(?:python)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


# Validator dispatch
_VALIDATORS: dict[str, callable] = {
    "validate_code_fizzbuzz": validate_code_fizzbuzz,
    "validate_code_reverse": validate_code_reverse,
    "validate_reasoning_logic": validate_reasoning_logic,
    "validate_exact_number": validate_exact_number,
    "validate_json_output": validate_json_output,
    "validate_numbered_list": validate_numbered_list,
    "validate_japanese_text": validate_japanese_text,
    "validate_japanese_summary": validate_japanese_summary,
}


class BenchmarkEngine:
    """Runs benchmark tests against Ollama models and caches results."""

    def __init__(
        self,
        client: OllamaClient,
        cache_path: Path = DEFAULT_CACHE_PATH,
    ):
        self.client = client
        self.cache_path = cache_path
        self._cache: dict[str, ModelBenchmark] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """Load cached benchmark results."""
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                for name, entry in data.items():
                    self._cache[name] = ModelBenchmark.from_dict(entry)
            except (json.JSONDecodeError, KeyError):
                self._cache = {}

    def _save_cache(self) -> None:
        """Save benchmark results to cache."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {name: bm.to_dict() for name, bm in self._cache.items()}
        self.cache_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_cached(self, model_name: str) -> ModelBenchmark | None:
        """Get cached benchmark for a model, or None."""
        return self._cache.get(model_name)

    def get_all_cached(self) -> dict[str, ModelBenchmark]:
        """Return all cached benchmarks."""
        return dict(self._cache)

    def get_unbenchmarked(self, installed_models: list[str]) -> list[str]:
        """Find models that are installed but not yet benchmarked."""
        return [m for m in installed_models if m not in self._cache]

    async def run_benchmark(
        self,
        model_name: str,
        *,
        timeout_per_test: float = 60.0,
        skip_categories: list[str] | None = None,
    ) -> ModelBenchmark:
        """Run full benchmark suite on a single model."""
        from datetime import datetime, timezone

        results: list[BenchmarkResult] = []

        for test in BENCHMARK_TESTS:
            if skip_categories and test.category in skip_categories:
                continue

            start = time.monotonic()
            try:
                response = await self.client.chat(
                    model=model_name,
                    messages=[{"role": "user", "content": test.prompt}],
                    temperature=0.0,
                )
                elapsed = time.monotonic() - start

                # Calculate tokens/sec estimate (rough: 1 token ≈ 4 chars)
                token_estimate = len(response) / 4
                tps = token_estimate / elapsed if elapsed > 0 else 0

                # Run validator
                validator_fn = _VALIDATORS.get(test.validator)
                if validator_fn is None:
                    passed, score = False, 0.0
                elif test.validator == "validate_exact_number":
                    passed, score = validator_fn(response, test.expected)
                else:
                    passed, score = validator_fn(response)

                results.append(BenchmarkResult(
                    category=test.category,
                    test_name=test.name,
                    passed=passed,
                    score=score,
                    response_time_sec=round(elapsed, 2),
                    tokens_per_sec=round(tps, 1),
                ))

            except Exception as e:
                elapsed = time.monotonic() - start
                results.append(BenchmarkResult(
                    category=test.category,
                    test_name=test.name,
                    passed=False,
                    score=0.0,
                    response_time_sec=round(elapsed, 2),
                    tokens_per_sec=0.0,
                ))

        # Aggregate scores
        benchmark = self._aggregate(model_name, results)
        benchmark.timestamp = datetime.now(timezone.utc).isoformat()

        # Cache
        self._cache[model_name] = benchmark
        self._save_cache()

        return benchmark

    def _aggregate(self, model_name: str, results: list[BenchmarkResult]) -> ModelBenchmark:
        """Aggregate individual test results into category scores and total."""
        category_scores: dict[str, list[float]] = {}
        all_tps: list[float] = []

        for r in results:
            category_scores.setdefault(r.category, []).append(r.score)
            if r.tokens_per_sec > 0:
                all_tps.append(r.tokens_per_sec)

        # Average score per category (0-100)
        cat_avg: dict[str, float] = {}
        for cat, scores in category_scores.items():
            cat_avg[cat] = round(sum(scores) / len(scores) * 100, 1)

        # Speed score: normalize tokens/sec to 0-100 scale
        avg_tps = sum(all_tps) / len(all_tps) if all_tps else 0
        # 30+ tps = 100, 0 tps = 0 (linear scale)
        speed_score = min(100.0, round(avg_tps / 30 * 100, 1))
        cat_avg["speed"] = speed_score

        # Weighted total
        total = 0.0
        weight_sum = 0.0
        for cat, weight in CATEGORY_WEIGHTS.items():
            if cat in cat_avg:
                total += cat_avg[cat] * weight
                weight_sum += weight
        if weight_sum > 0:
            total = round(total / weight_sum * weight_sum / sum(CATEGORY_WEIGHTS.values()), 1)
            # Normalize to 0-100
            total = round(total / weight_sum * 100, 1) if weight_sum > 0 else 0.0
            # Simpler: weighted average
            total = round(sum(cat_avg.get(c, 0) * w for c, w in CATEGORY_WEIGHTS.items()) / sum(CATEGORY_WEIGHTS.values()), 1)

        return ModelBenchmark(
            model_name=model_name,
            total_score=total,
            category_scores=cat_avg,
            avg_tokens_per_sec=round(avg_tps, 1),
            results=[
                {
                    "category": r.category,
                    "test": r.test_name,
                    "passed": r.passed,
                    "score": r.score,
                    "time_sec": r.response_time_sec,
                    "tps": r.tokens_per_sec,
                }
                for r in results
            ],
        )

    def remove_cached(self, model_name: str) -> bool:
        """Remove cached benchmark for a model."""
        if model_name in self._cache:
            del self._cache[model_name]
            self._save_cache()
            return True
        return False

    def clear_cache(self) -> int:
        """Clear all cached benchmarks. Returns count of removed entries."""
        count = len(self._cache)
        self._cache.clear()
        self._save_cache()
        return count
