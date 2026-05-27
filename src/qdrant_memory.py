"""Qdrant shared memory client using httpx (no qdrant-client dependency)."""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
import uuid
from dataclasses import dataclass

import httpx

from .ollama_client import OllamaClient


@dataclass
class QdrantMemoryConfig:
    qdrant_url: str = os.environ.get("QDRANT_URL", "http://localhost:6333")
    collection: str = "mem0_shared"
    embedding_model: str = "qwen3-embedding:8b"
    embedding_dim: int = 4096
    ollama_host: str = os.environ.get("HELIX_OLLAMA_HOST", os.environ.get("OLLAMA_EMBED_HOST", "http://localhost:11434"))
    user_id: str = os.environ.get("HELIX_USER_ID", "default")
    top_k: int = 5
    score_threshold: float = 0.3
    api_key: str = os.environ.get("QDRANT_API_KEY", "").strip()
    sparse_ngram_range: tuple[int, int] = (2, 4)
    sparse_vocab_size: int = 50_000


class QdrantMemory:
    """Search and store memories in Qdrant via HTTP API."""

    def __init__(self, config: QdrantMemoryConfig | None = None):
        self.config = config or QdrantMemoryConfig()
        self._ollama = OllamaClient(host=self.config.ollama_host)
        self._headers: dict[str, str] = {}
        if self.config.api_key:
            self._headers["api-key"] = self.config.api_key
        self._sparse_field_cache: dict[str, bool] = {}

    async def _has_sparse_field(self, collection: str) -> bool:
        """Check if collection has sparse vector field (cached per session)."""
        if collection in self._sparse_field_cache:
            return self._sparse_field_cache[collection]
        result = await self.ensure_sparse_field(collection)
        self._sparse_field_cache[collection] = result
        return result

    async def _embed(self, text: str) -> list[float]:
        embeddings = await self._ollama.embeddings(self.config.embedding_model, text)
        if not embeddings:
            raise RuntimeError("Embedding returned empty result")
        return embeddings[0]

    # ── Sparse vector encoder (char N-gram + TF) ──

    _PUNCT_RE = re.compile(r"[\s　​﻿]+")
    _STRIP_RE = re.compile(r"[。、，．．！？!?,.\-;:\"'()（）「」『』【】\[\]{}<>…―─\n\r\t]+")

    def _sparse_encode(self, text: str) -> tuple[list[int], list[float]]:
        """文字 N-gram (2-4gram) ベースの sparse vector.

        日本語は分かち書き不要 — 文字 N-gram が部分一致検索として機能する。
        Returns (indices, values) where indices are hashed N-gram IDs and
        values are TF (term frequency) weights with sub-linear scaling.
        """
        lo, hi = self.config.sparse_ngram_range
        vocab = self.config.sparse_vocab_size

        # 正規化: 小文字化 + 句読点/空白除去
        t = self._STRIP_RE.sub("", self._PUNCT_RE.sub(" ", text.lower()))
        tokens = t.split()  # whitespace split (英語語境界 + 日本語は1塊)

        counts: dict[int, int] = {}
        for token in tokens:
            for n in range(lo, hi + 1):
                for i in range(len(token) - n + 1):
                    gram = token[i : i + n]
                    h = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16) % vocab
                    counts[h] = counts.get(h, 0) + 1

        if not counts:
            return ([], [])

        # Sub-linear TF: 1 + log(tf) to avoid domination by repeated terms
        indices = sorted(counts.keys())
        values = [1.0 + math.log(counts[idx]) if counts[idx] > 1 else 1.0 for idx in indices]
        return (indices, values)

    async def ensure_sparse_field(self, collection: str | None = None) -> bool:
        """Check whether the collection has a sparse vector field 'sparse'.

        Returns True if the field already exists. Qdrant does NOT support
        adding sparse vectors to an existing collection via API — use
        scripts/migrate_sparse.py to recreate the collection with sparse support.
        """
        coll = collection or self.config.collection
        # まずコレクション情報を取得して sparse field の有無を確認
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=self._headers) as client:
                r = await client.get(f"{self.config.qdrant_url}/collections/{coll}")
                r.raise_for_status()
                info = r.json()
            vectors_config = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
            # sparse vectors は別キー
            sparse_config = info.get("result", {}).get("config", {}).get("params", {}).get("sparse_vectors", {})
            if "sparse" in sparse_config:
                return True  # 既に存在
        except Exception:
            return False

        # sparse field を追加
        try:
            payload = {
                "sparse_vectors": {
                    "sparse": {}  # Qdrant は modifier 不要でデフォルト設定
                }
            }
            await self._qdrant_post(
                f"/collections/{coll}",
                payload,
                method="PUT",  # PATCH semantic — Qdrant は PUT で部分更新
                timeout=15.0,
            )
            return True
        except Exception:
            return False

    async def _qdrant_post(self, path: str, payload: dict, timeout: float = 15.0, method: str = "POST") -> dict:
        url = f"{self.config.qdrant_url}{path}"
        async with httpx.AsyncClient(timeout=timeout, headers=self._headers) as client:
            if method == "PUT":
                r = await client.put(url, json=payload)
            else:
                r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=self._headers) as client:
                r = await client.get(f"{self.config.qdrant_url}/collections/{self.config.collection}")
                return r.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        source: str | None = None,
        category: str | None = None,
        collection: str | None = None,
        hybrid: bool = False,
    ) -> list[dict]:
        vector = await self._embed(query)
        k = top_k or self.config.top_k
        coll = collection or self.config.collection

        must_filters = [
            {"key": "user_id", "match": {"value": self.config.user_id}}
        ]
        if source:
            must_filters.append({"key": "source", "match": {"value": source}})
        if category:
            must_filters.append({"key": "category", "match": {"value": category}})

        qdrant_filter = {"must": must_filters}

        if hybrid:
            result = await self._hybrid_query(vector, k, coll, qdrant_filter, query_text=query)
        else:
            result = await self._dense_search(vector, k, coll, qdrant_filter)

        return self._parse_search_results(result, hybrid=hybrid)

    async def _dense_search(
        self,
        vector: list[float],
        limit: int,
        collection: str,
        qdrant_filter: dict,
    ) -> dict:
        """既存の dense vector 検索 (POST /points/search)."""
        has_sparse = await self._has_sparse_field(collection)
        vec_value: dict | list[float] = {"name": "dense", "vector": vector} if has_sparse else vector
        payload = {
            "vector": vec_value,
            "limit": limit,
            "score_threshold": self.config.score_threshold,
            "filter": qdrant_filter,
            "with_payload": True,
        }
        return await self._qdrant_post(
            f"/collections/{collection}/points/search",
            payload,
        )

    async def _hybrid_query(
        self,
        vector: list[float],
        limit: int,
        collection: str,
        qdrant_filter: dict,
        query_text: str = "",
    ) -> dict:
        """Hybrid search via Query API (POST /points/query).

        Phase 2: dense + sparse prefetch → RRF fusion.
        sparse vector は文字 N-gram ベースで生成。
        """
        has_sparse = await self._has_sparse_field(collection)
        dense_prefetch: dict = {
            "query": vector,
            "limit": limit * 3,
            "filter": qdrant_filter,
        }
        if has_sparse:
            dense_prefetch["using"] = "dense"
        prefetch = [dense_prefetch]
        if has_sparse and query_text:
            indices, values = self._sparse_encode(query_text)
            if indices:
                prefetch.append({
                    "query": {"indices": indices, "values": values},
                    "using": "sparse",
                    "limit": limit * 3,
                    "filter": qdrant_filter,
                })

        payload = {
            "prefetch": prefetch,
            "query": {"fusion": "rrf"},
            "limit": limit,
            "with_payload": True,
            "filter": qdrant_filter,
        }
        return await self._qdrant_post(
            f"/collections/{collection}/points/query",
            payload,
        )

    @staticmethod
    def _parse_search_results(result: dict, *, hybrid: bool = False) -> list[dict]:
        """search / query 両 API のレスポンスを統一フォーマットに変換."""
        # Query API は "result" 内に "points" キー、Search API は "result" が直接リスト
        raw = result.get("result", [])
        if hybrid and isinstance(raw, dict):
            points = raw.get("points", [])
        elif isinstance(raw, list):
            points = raw
        else:
            points = []

        hits = []
        for point in points:
            p = point.get("payload", {})
            hits.append({
                "text": p.get("data", p.get("text", p.get("memory", ""))),
                "score": round(point.get("score", 0.0), 4),
                "created_at": p.get("created_at", ""),
                "source": p.get("source", ""),
            })
        return hits

    async def add(self, text: str, metadata: dict | None = None, collection: str | None = None) -> str:
        vector = await self._embed(text)
        point_id = str(uuid.uuid4())
        coll = collection or self.config.collection

        payload = {
            "data": text,
            "user_id": self.config.user_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "helix-agent",
        }
        if metadata:
            payload.update(metadata)

        # Build vector: use named vectors only if collection supports sparse
        has_sparse = await self._has_sparse_field(coll)
        sparse_indices, sparse_values = self._sparse_encode(text) if has_sparse else ([], [])
        point_vectors: dict | list[float]
        if has_sparse and sparse_indices:
            point_vectors = {
                "dense": vector,
                "sparse": {"indices": sparse_indices, "values": sparse_values},
            }
        else:
            point_vectors = vector

        upsert_payload = {
            "points": [
                {
                    "id": point_id,
                    "vector": point_vectors,
                    "payload": payload,
                }
            ]
        }

        try:
            await self._qdrant_post(
                f"/collections/{coll}/points",
                upsert_payload,
                timeout=15.0,
                method="PUT",
            )
            self._append_canonical_log(point_id, coll, text, payload, status="stored")
            return point_id
        except Exception:
            self._spool_to_jsonl(text, payload, coll, vector, point_id=point_id)
            self._append_canonical_log(point_id, coll, text, payload, status="spooled")
            return f"spool:{point_id}"

    def _spool_to_jsonl(self, text: str, payload: dict, collection: str, vector: list[float], point_id: str = "") -> None:
        """Qdrant 接続失敗時にローカル JSONL に蓄積 (後で replay)."""
        import json as _json
        from pathlib import Path as _Path
        spool_dir = _Path.home() / ".claude" / "qdrant_spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        spool_file = spool_dir / f"spool_{collection}.jsonl"
        entry = {
            "collection": collection,
            "payload": payload,
            "vector_dim": len(vector),
            "point_id": point_id,
            "data_preview": text[:500],
            "spooled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(spool_file, "a") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def _append_canonical_log(point_id: str, collection: str, text: str, payload: dict, status: str = "pending") -> None:
        """全記憶書き込みを append-only JSONL に記録 (Qdrant 成功/失敗を問わず正本として保持)."""
        import json as _json
        from pathlib import Path as _Path
        log_path = _Path.home() / ".claude" / "memory_events.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "point_id": point_id,
            "collection": collection,
            "data": text,
            "status": status,
            "metadata": {k: v for k, v in payload.items() if k != "data"},
        }
        try:
            with open(log_path, "a") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
