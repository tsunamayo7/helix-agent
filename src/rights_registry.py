"""Rights registry — SQLite-backed asset license tracking."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Default DB path
DEFAULT_DB_DIR = Path.home() / ".helix-agent" / "corp"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "rights.db"

VALID_TYPES = {"model", "image", "audio", "video", "font", "code"}
VALID_SOURCES = {"original", "generated", "third_party", "oss"}

# Licenses known to allow commercial use without special conditions
_PERMISSIVE_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unlicense"}

# Licenses that require attribution
_ATTRIBUTION_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC-BY", "CC-BY-SA"}

# Copyleft licenses — derivative works must use same license
_COPYLEFT_LICENSES = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0", "CC-BY-SA"}

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS assets (
    asset_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    source      TEXT NOT NULL,
    name        TEXT NOT NULL,
    license     TEXT NOT NULL,
    attribution TEXT NOT NULL DEFAULT '',
    restrictions TEXT NOT NULL DEFAULT '[]',
    url         TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
)
"""

_INITIAL_ASSETS = [
    {
        "type": "code",
        "source": "oss",
        "name": "Open-LLM-VTuber",
        "license": "MIT",
        "attribution": "t41372/Open-LLM-VTuber",
        "restrictions": [],
        "url": "https://github.com/t41372/Open-LLM-VTuber",
    },
    {
        "type": "code",
        "source": "oss",
        "name": "Irodori-TTS",
        "license": "Apache-2.0",
        "attribution": "Irodori-ai/Irodori-TTS",
        "restrictions": [],
        "url": "https://github.com/Irodori-ai/Irodori-TTS",
    },
    {
        "type": "code",
        "source": "oss",
        "name": "pyiqa",
        "license": "Apache-2.0",
        "attribution": "chaofengc/IQA-PyTorch",
        "restrictions": [],
        "url": "https://github.com/chaofengc/IQA-PyTorch",
    },
    {
        "type": "code",
        "source": "oss",
        "name": "ComfyUI",
        "license": "GPL-3.0",
        "attribution": "comfyanonymous/ComfyUI",
        "restrictions": ["copyleft", "derivative_same_license"],
        "url": "https://github.com/comfyanonymous/ComfyUI",
    },
    {
        "type": "model",
        "source": "oss",
        "name": "See-Through",
        "license": "custom",
        "attribution": "See-Through authors",
        "restrictions": ["license_unconfirmed", "verify_before_commercial"],
        "url": "",
    },
    {
        "type": "code",
        "source": "oss",
        "name": "Ollama",
        "license": "MIT",
        "attribution": "ollama/ollama",
        "restrictions": [],
        "url": "https://github.com/ollama/ollama",
    },
]


class RightsRegistry:
    """Manage asset license information in a local SQLite database."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def register_asset(
        self,
        type: str,
        source: str,
        name: str,
        license: str,
        restrictions: list[str] | None = None,
        url: str = "",
        attribution: str = "",
    ) -> str:
        """Register an asset and return its generated asset_id."""
        if type not in VALID_TYPES:
            raise ValueError(f"Invalid type '{type}'. Must be one of {VALID_TYPES}")
        if source not in VALID_SOURCES:
            raise ValueError(f"Invalid source '{source}'. Must be one of {VALID_SOURCES}")
        if not name:
            raise ValueError("name must not be empty")
        if not license:
            raise ValueError("license must not be empty")

        asset_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        restrictions_json = json.dumps(restrictions or [])

        self._conn.execute(
            """INSERT INTO assets
               (asset_id, type, source, name, license, attribution, restrictions, url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (asset_id, type, source, name, license, attribution, restrictions_json, url, now),
        )
        self._conn.commit()
        return asset_id

    def check_asset(self, asset_id: str) -> dict[str, Any]:
        """Look up an asset and return its details with a usage verdict."""
        row = self._conn.execute(
            "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Asset not found: {asset_id}")

        data = dict(row)
        data["restrictions"] = json.loads(data["restrictions"])

        # Derive usage verdict
        data["commercial_ok"] = self._is_commercial_ok(data["license"], data["restrictions"])
        data["attribution_required"] = self._needs_attribution(data["license"], data["restrictions"])
        data["copyleft"] = data["license"] in _COPYLEFT_LICENSES
        return data

    def list_assets(self, type: str | None = None) -> list[dict[str, Any]]:
        """List all assets, optionally filtered by type."""
        if type is not None and type not in VALID_TYPES:
            raise ValueError(f"Invalid type '{type}'. Must be one of {VALID_TYPES}")

        if type is None:
            rows = self._conn.execute("SELECT * FROM assets ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE type = ? ORDER BY created_at", (type,)
            ).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            d["restrictions"] = json.loads(d["restrictions"])
            results.append(d)
        return results

    def export_csv(self) -> str:
        """Export all assets as a CSV string."""
        rows = self._conn.execute("SELECT * FROM assets ORDER BY created_at").fetchall()
        if not rows:
            return ""

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
        return output.getvalue()

    # ------------------------------------------------------------------
    # Seed data
    # ------------------------------------------------------------------

    def seed_initial_data(self) -> list[str]:
        """Insert initial OSS dependency records. Returns list of created asset_ids.

        Skips assets whose name already exists to allow safe re-seeding.
        """
        created: list[str] = []
        for asset in _INITIAL_ASSETS:
            existing = self._conn.execute(
                "SELECT asset_id FROM assets WHERE name = ?", (asset["name"],)
            ).fetchone()
            if existing:
                continue
            aid = self.register_asset(
                type=asset["type"],
                source=asset["source"],
                name=asset["name"],
                license=asset["license"],
                restrictions=asset["restrictions"],
                url=asset["url"],
                attribution=asset["attribution"],
            )
            created.append(aid)
        return created

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_commercial_ok(license_id: str, restrictions: list[str]) -> bool:
        if "no_commercial" in restrictions:
            return False
        if "verify_before_commercial" in restrictions:
            return False
        if license_id in _PERMISSIVE_LICENSES:
            return True
        if license_id in _COPYLEFT_LICENSES:
            # Copyleft allows commercial, but derivative must use same license
            return True
        if license_id.startswith("CC-BY"):
            return "NC" not in license_id
        if license_id in {"proprietary", "custom"}:
            return False
        return False

    @staticmethod
    def _needs_attribution(license_id: str, restrictions: list[str]) -> bool:
        if "attribution_required" in restrictions:
            return True
        return license_id in _ATTRIBUTION_LICENSES
