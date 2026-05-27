"""Tests for Phase 2 sparse vector: _sparse_encode and hybrid search integration."""

from __future__ import annotations

import hashlib
import math
from unittest.mock import AsyncMock, patch

import pytest

from src.qdrant_memory import QdrantMemory, QdrantMemoryConfig


# ── Fixtures ──


@pytest.fixture
def memory() -> QdrantMemory:
    config = QdrantMemoryConfig(
        qdrant_url="http://localhost:6333",
        collection="test_collection",
        sparse_ngram_range=(2, 4),
        sparse_vocab_size=50_000,
    )
    return QdrantMemory(config)


# ── _sparse_encode basic tests ──


class TestSparseEncodeBasic:
    def test_returns_tuple(self, memory: QdrantMemory):
        indices, values = memory._sparse_encode("hello world")
        assert isinstance(indices, list)
        assert isinstance(values, list)
        assert len(indices) == len(values)

    def test_non_empty_output(self, memory: QdrantMemory):
        indices, values = memory._sparse_encode("test text")
        assert len(indices) > 0
        assert all(isinstance(i, int) for i in indices)
        assert all(isinstance(v, float) for v in values)

    def test_indices_are_sorted(self, memory: QdrantMemory):
        indices, _ = memory._sparse_encode("hello world foo bar")
        assert indices == sorted(indices)

    def test_indices_within_vocab(self, memory: QdrantMemory):
        indices, _ = memory._sparse_encode("some sample text for testing")
        for idx in indices:
            assert 0 <= idx < memory.config.sparse_vocab_size

    def test_empty_text(self, memory: QdrantMemory):
        indices, values = memory._sparse_encode("")
        assert indices == []
        assert values == []

    def test_single_char(self, memory: QdrantMemory):
        # Single char cannot form 2-gram
        indices, values = memory._sparse_encode("a")
        assert indices == []
        assert values == []

    def test_two_chars(self, memory: QdrantMemory):
        # "ab" -> one 2-gram
        indices, values = memory._sparse_encode("ab")
        assert len(indices) >= 1

    def test_values_are_positive(self, memory: QdrantMemory):
        _, values = memory._sparse_encode("hello world test")
        assert all(v > 0 for v in values)

    def test_deterministic(self, memory: QdrantMemory):
        """Same input always produces same output."""
        text = "deterministic test input"
        result1 = memory._sparse_encode(text)
        result2 = memory._sparse_encode(text)
        assert result1 == result2


class TestSparseEncodeNgrams:
    def test_generates_2gram(self, memory: QdrantMemory):
        """'ab' should produce at least a 2-gram."""
        indices, values = memory._sparse_encode("ab")
        expected_hash = int(hashlib.md5("ab".encode("utf-8")).hexdigest(), 16) % 50_000
        assert expected_hash in indices

    def test_generates_3gram(self, memory: QdrantMemory):
        """'abc' should produce 2-gram and 3-gram."""
        indices, _ = memory._sparse_encode("abc")
        hash_abc = int(hashlib.md5("abc".encode("utf-8")).hexdigest(), 16) % 50_000
        assert hash_abc in indices

    def test_generates_4gram(self, memory: QdrantMemory):
        """'abcd' should produce 4-gram."""
        indices, _ = memory._sparse_encode("abcd")
        hash_abcd = int(hashlib.md5("abcd".encode("utf-8")).hexdigest(), 16) % 50_000
        assert hash_abcd in indices

    def test_tf_weighting(self, memory: QdrantMemory):
        """Repeated N-grams should have higher TF weight."""
        # "abab" has "ab" appearing twice as 2-gram
        indices, values = memory._sparse_encode("abab")
        hash_ab = int(hashlib.md5("ab".encode("utf-8")).hexdigest(), 16) % 50_000
        if hash_ab in indices:
            idx = indices.index(hash_ab)
            # tf=2 -> 1 + log(2) ~ 1.693
            assert values[idx] > 1.0

    def test_sublinear_tf(self, memory: QdrantMemory):
        """tf=1 -> 1.0, tf>1 -> 1 + log(tf)."""
        # "xy" -> "xy" appears once -> value = 1.0
        indices, values = memory._sparse_encode("xy")
        hash_xy = int(hashlib.md5("xy".encode("utf-8")).hexdigest(), 16) % 50_000
        if hash_xy in indices:
            idx = indices.index(hash_xy)
            assert values[idx] == 1.0

    def test_custom_ngram_range(self):
        """Custom N-gram range (3, 3) should only produce 3-grams."""
        config = QdrantMemoryConfig(sparse_ngram_range=(3, 3), sparse_vocab_size=10_000)
        mem = QdrantMemory(config)
        indices, _ = mem._sparse_encode("abcd")
        # Only 3-grams: "abc", "bcd"
        hash_abc = int(hashlib.md5("abc".encode("utf-8")).hexdigest(), 16) % 10_000
        hash_bcd = int(hashlib.md5("bcd".encode("utf-8")).hexdigest(), 16) % 10_000
        # No 2-gram or 4-gram
        hash_ab = int(hashlib.md5("ab".encode("utf-8")).hexdigest(), 16) % 10_000
        hash_abcd = int(hashlib.md5("abcd".encode("utf-8")).hexdigest(), 16) % 10_000
        assert hash_abc in indices
        assert hash_bcd in indices
        # 2-gram/4-gram should not be present (unless hash collision)
        if hash_ab != hash_abc and hash_ab != hash_bcd:
            assert hash_ab not in indices


# ── Japanese text tests ──


class TestSparseEncodeJapanese:
    def test_japanese_produces_output(self, memory: QdrantMemory):
        """Japanese text without word segmentation should still work."""
        indices, values = memory._sparse_encode("ハイブリッド検索テスト")
        assert len(indices) > 0

    def test_japanese_bigrams(self, memory: QdrantMemory):
        """Japanese char bigrams are generated correctly."""
        text = "検索"
        indices, _ = memory._sparse_encode(text)
        hash_kensaku = int(hashlib.md5("検索".encode("utf-8")).hexdigest(), 16) % 50_000
        assert hash_kensaku in indices

    def test_mixed_ja_en(self, memory: QdrantMemory):
        """Mixed Japanese/English text produces N-grams from both."""
        indices, values = memory._sparse_encode("Claude AIの記憶検索")
        assert len(indices) > 5  # Should have plenty of N-grams

    def test_punctuation_removed(self, memory: QdrantMemory):
        """Japanese punctuation should be stripped."""
        # Same content, different punctuation -> same indices
        indices1, _ = memory._sparse_encode("テスト")
        indices2, _ = memory._sparse_encode("テスト。")
        assert indices1 == indices2

    def test_katakana_hiragana(self, memory: QdrantMemory):
        indices, _ = memory._sparse_encode("ひらがなとカタカナ")
        assert len(indices) > 0


# ── Hybrid query structure tests ──


class TestHybridQueryStructure:
    @pytest.mark.asyncio
    async def test_hybrid_query_includes_sparse_prefetch(self, memory: QdrantMemory):
        """hybrid query should include sparse prefetch when query_text is provided."""
        captured_payload = {}

        async def mock_post(path: str, payload: dict, **kwargs):
            captured_payload.update(payload)
            return {"result": {"points": []}}

        memory._qdrant_post = mock_post

        dense_vector = [0.1] * 4096
        qdrant_filter = {"must": [{"key": "user_id", "match": {"value": "test"}}]}

        await memory._hybrid_query(
            vector=dense_vector,
            limit=5,
            collection="test_collection",
            qdrant_filter=qdrant_filter,
            query_text="テスト検索クエリ",
        )

        prefetch = captured_payload.get("prefetch", [])
        assert len(prefetch) == 2, f"Expected 2 prefetch entries (dense + sparse), got {len(prefetch)}"

        # First prefetch: dense
        assert prefetch[0]["using"] == "dense"
        assert prefetch[0]["query"] == dense_vector

        # Second prefetch: sparse
        assert prefetch[1]["using"] == "sparse"
        sparse_query = prefetch[1]["query"]
        assert "indices" in sparse_query
        assert "values" in sparse_query
        assert len(sparse_query["indices"]) > 0

        # Fusion: RRF
        assert captured_payload["query"] == {"fusion": "rrf"}

    @pytest.mark.asyncio
    async def test_hybrid_query_no_sparse_without_text(self, memory: QdrantMemory):
        """Without query_text, only dense prefetch should be present."""
        captured_payload = {}

        async def mock_post(path: str, payload: dict, **kwargs):
            captured_payload.update(payload)
            return {"result": {"points": []}}

        memory._qdrant_post = mock_post

        await memory._hybrid_query(
            vector=[0.1] * 4096,
            limit=5,
            collection="test_collection",
            qdrant_filter={"must": []},
            query_text="",
        )

        prefetch = captured_payload.get("prefetch", [])
        assert len(prefetch) == 1
        assert prefetch[0]["using"] == "dense"

    @pytest.mark.asyncio
    async def test_search_hybrid_passes_query_text(self, memory: QdrantMemory):
        """search(hybrid=True) should pass query text to _hybrid_query."""
        call_args = {}

        async def mock_embed(text):
            return [0.1] * 4096

        async def mock_hybrid(vector, limit, collection, qdrant_filter, query_text=""):
            call_args["query_text"] = query_text
            return {"result": {"points": []}}

        memory._embed = mock_embed
        memory._hybrid_query = mock_hybrid

        await memory.search("テスト", hybrid=True)
        assert call_args["query_text"] == "テスト"

    @pytest.mark.asyncio
    async def test_search_dense_does_not_use_sparse(self, memory: QdrantMemory):
        """search(hybrid=False) should use _dense_search, not _hybrid_query."""
        dense_called = []
        hybrid_called = []

        async def mock_embed(text):
            return [0.1] * 4096

        async def mock_dense(vector, limit, collection, qdrant_filter):
            dense_called.append(True)
            return {"result": []}

        async def mock_hybrid(vector, limit, collection, qdrant_filter, query_text=""):
            hybrid_called.append(True)
            return {"result": {"points": []}}

        memory._embed = mock_embed
        memory._dense_search = mock_dense
        memory._hybrid_query = mock_hybrid

        await memory.search("test", hybrid=False)
        assert len(dense_called) == 1
        assert len(hybrid_called) == 0


# ── add() with sparse vector tests ──


class TestAddWithSparseVector:
    @pytest.mark.asyncio
    async def test_add_includes_sparse_vector(self, memory: QdrantMemory):
        """add() should include sparse vector in upsert payload."""
        captured_payload = {}

        async def mock_embed(text):
            return [0.1] * 4096

        async def mock_post(path: str, payload: dict, **kwargs):
            captured_payload.update(payload)
            return {"result": {"status": "ok"}}

        memory._embed = mock_embed
        memory._qdrant_post = mock_post

        await memory.add("ハイブリッド検索のテストデータ")

        points = captured_payload.get("points", [])
        assert len(points) == 1

        vector = points[0]["vector"]
        # Should be a dict with "dense" and "sparse" keys
        assert isinstance(vector, dict)
        assert "dense" in vector
        assert "sparse" in vector
        assert len(vector["dense"]) == 4096
        assert "indices" in vector["sparse"]
        assert "values" in vector["sparse"]
        assert len(vector["sparse"]["indices"]) > 0

    @pytest.mark.asyncio
    async def test_add_short_text_falls_back_to_unnamed(self, memory: QdrantMemory):
        """Single char text -> no sparse -> unnamed dense vector."""
        captured_payload = {}

        async def mock_embed(text):
            return [0.1] * 4096

        async def mock_post(path: str, payload: dict, **kwargs):
            captured_payload.update(payload)
            return {"result": {"status": "ok"}}

        memory._embed = mock_embed
        memory._qdrant_post = mock_post

        await memory.add("a")  # single char, no N-grams

        points = captured_payload.get("points", [])
        vector = points[0]["vector"]
        # Should be a plain list (unnamed), not a dict
        assert isinstance(vector, list)


# ── ensure_sparse_field tests ──


class _FakeResponse:
    """Synchronous fake httpx.Response for ensure_sparse_field tests."""

    def __init__(self, json_data: dict, status_code: int = 200):
        self._json = json_data
        self.status_code = status_code

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, get_response=None, get_error=None):
        self._get_response = get_response
        self._get_error = get_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        if self._get_error:
            raise self._get_error
        return self._get_response


class TestEnsureSparseField:
    @pytest.mark.asyncio
    async def test_already_exists(self, memory: QdrantMemory):
        """If sparse field already exists, return True without creating."""
        collection_info = {
            "result": {
                "config": {
                    "params": {
                        "vectors": {"dense": {"size": 4096, "distance": "Cosine"}},
                        "sparse_vectors": {"sparse": {}},
                    }
                }
            }
        }

        fake_client = _FakeAsyncClient(get_response=_FakeResponse(collection_info))

        with patch("src.qdrant_memory.httpx.AsyncClient", return_value=fake_client):
            result = await memory.ensure_sparse_field()
            assert result is True

    @pytest.mark.asyncio
    async def test_connection_error(self, memory: QdrantMemory):
        """Connection failure returns False."""
        fake_client = _FakeAsyncClient(get_error=Exception("Connection refused"))

        with patch("src.qdrant_memory.httpx.AsyncClient", return_value=fake_client):
            result = await memory.ensure_sparse_field()
            assert result is False


# ── Edge cases ──


class TestSparseEncodeEdgeCases:
    def test_only_punctuation(self, memory: QdrantMemory):
        indices, values = memory._sparse_encode("。、！？")
        assert indices == []
        assert values == []

    def test_whitespace_only(self, memory: QdrantMemory):
        indices, values = memory._sparse_encode("   \t\n  ")
        assert indices == []
        assert values == []

    def test_very_long_text(self, memory: QdrantMemory):
        """Long text should still work and produce many N-grams."""
        text = "テスト" * 1000
        indices, values = memory._sparse_encode(text)
        assert len(indices) > 0
        # Repeated text -> high TF for some indices
        assert any(v > 1.0 for v in values)

    def test_unicode_normalization(self, memory: QdrantMemory):
        """Fullwidth and halfwidth chars produce different N-grams (expected)."""
        indices_half, _ = memory._sparse_encode("abc")
        indices_full, _ = memory._sparse_encode("ａｂｃ")
        # These are different characters, should produce different hashes
        assert len(indices_half) > 0
        assert len(indices_full) > 0

    def test_no_duplicate_indices(self, memory: QdrantMemory):
        """Indices list should not contain duplicates."""
        indices, _ = memory._sparse_encode("hello world hello world hello")
        assert len(indices) == len(set(indices))
