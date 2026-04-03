"""Qdrant shared memory client using httpx (no qdrant-client dependency)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import httpx

from .ollama_client import OllamaClient


@dataclass
class QdrantMemoryConfig:
    qdrant_url: str = "http://localhost:6333"
    collection: str = "mem0_shared"
    embedding_model: str = "qwen3-embedding:8b"
    embedding_dim: int = 4096
    ollama_host: str = "http://localhost:11434"
    user_id: str = "tsunamayo7"
    top_k: int = 5
    score_threshold: float = 0.3


class QdrantMemory:
    """Search and store memories in Qdrant via HTTP API."""

    def __init__(self, config: QdrantMemoryConfig | None = None):
        self.config = config or QdrantMemoryConfig()
        self._ollama = OllamaClient(host=self.config.ollama_host)

    async def _embed(self, text: str) -> list[float]:
        embeddings = await self._ollama.embeddings(self.config.embedding_model, text)
        if not embeddings:
            raise RuntimeError("Embedding returned empty result")
        return embeddings[0]

    async def _qdrant_post(self, path: str, payload: dict, timeout: float = 15.0) -> dict:
        url = f"{self.config.qdrant_url}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.config.qdrant_url}/collections/{self.config.collection}")
                return r.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def search(self, query: str, top_k: int | None = None) -> list[dict]:
        vector = await self._embed(query)
        k = top_k or self.config.top_k

        payload = {
            "vector": vector,
            "limit": k,
            "score_threshold": self.config.score_threshold,
            "filter": {
                "must": [
                    {"key": "user_id", "match": {"value": self.config.user_id}}
                ]
            },
            "with_payload": True,
        }

        result = await self._qdrant_post(
            f"/collections/{self.config.collection}/points/search",
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

    async def add(self, text: str, metadata: dict | None = None) -> str:
        vector = await self._embed(text)
        point_id = str(uuid.uuid4())

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

        await self._qdrant_post(
            f"/collections/{self.config.collection}/points",
            upsert_payload,
            timeout=15.0,
        )
        return point_id
