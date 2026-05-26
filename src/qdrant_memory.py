"""Qdrant shared memory client using httpx (no qdrant-client dependency)."""

from __future__ import annotations

import os
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
    ollama_host: str = os.environ.get("HELIX_OLLAMA_HOST", os.environ.get("OLLAMA_EMBED_HOST", "http://tsunamayo-1:11434"))
    user_id: str = os.environ.get("HELIX_USER_ID", "default")
    top_k: int = 5
    score_threshold: float = 0.3
    api_key: str = os.environ.get("QDRANT_API_KEY", "").strip()


class QdrantMemory:
    """Search and store memories in Qdrant via HTTP API."""

    def __init__(self, config: QdrantMemoryConfig | None = None):
        self.config = config or QdrantMemoryConfig()
        self._ollama = OllamaClient(host=self.config.ollama_host)
        self._headers: dict[str, str] = {}
        if self.config.api_key:
            self._headers["api-key"] = self.config.api_key

    async def _embed(self, text: str) -> list[float]:
        embeddings = await self._ollama.embeddings(self.config.embedding_model, text)
        if not embeddings:
            raise RuntimeError("Embedding returned empty result")
        return embeddings[0]

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

        payload = {
            "vector": vector,
            "limit": k,
            "score_threshold": self.config.score_threshold,
            "filter": {"must": must_filters},
            "with_payload": True,
        }

        result = await self._qdrant_post(
            f"/collections/{coll}/points/search",
            payload,
        )

        hits = []
        for point in result.get("result", []):
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

        upsert_payload = {
            "points": [
                {
                    "id": point_id,
                    "vector": vector,
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
        except Exception:
            self._spool_to_jsonl(text, payload, coll, vector)
        return point_id

    def _spool_to_jsonl(self, text: str, payload: dict, collection: str, vector: list[float]) -> None:
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
            "text_preview": text[:200],
            "spooled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(spool_file, "a") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
