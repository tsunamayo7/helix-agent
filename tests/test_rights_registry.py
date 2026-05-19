"""Tests for rights registry module."""

import json
import tempfile
from pathlib import Path

import pytest

from src.rights_registry import RightsRegistry, VALID_TYPES, VALID_SOURCES


@pytest.fixture
def registry(tmp_path: Path) -> RightsRegistry:
    """Create a fresh registry with a temp DB for each test."""
    db = tmp_path / "test_rights.db"
    reg = RightsRegistry(db_path=db)
    yield reg
    reg.close()


@pytest.fixture
def seeded_registry(registry: RightsRegistry) -> RightsRegistry:
    """Registry with initial OSS data loaded."""
    registry.seed_initial_data()
    return registry


class TestRegisterAsset:
    def test_register_returns_uuid(self, registry: RightsRegistry):
        aid = registry.register_asset(
            type="code", source="oss", name="test-lib", license="MIT"
        )
        assert isinstance(aid, str)
        assert len(aid) == 36  # UUID format

    def test_register_with_all_fields(self, registry: RightsRegistry):
        aid = registry.register_asset(
            type="model",
            source="third_party",
            name="some-model",
            license="Apache-2.0",
            restrictions=["attribution_required"],
            url="https://example.com",
            attribution="Author Name",
        )
        asset = registry.check_asset(aid)
        assert asset["name"] == "some-model"
        assert asset["type"] == "model"
        assert asset["source"] == "third_party"
        assert asset["license"] == "Apache-2.0"
        assert asset["attribution"] == "Author Name"
        assert asset["url"] == "https://example.com"
        assert "attribution_required" in asset["restrictions"]

    def test_register_invalid_type(self, registry: RightsRegistry):
        with pytest.raises(ValueError, match="Invalid type"):
            registry.register_asset(type="unknown", source="oss", name="x", license="MIT")

    def test_register_invalid_source(self, registry: RightsRegistry):
        with pytest.raises(ValueError, match="Invalid source"):
            registry.register_asset(type="code", source="invalid", name="x", license="MIT")

    def test_register_empty_name(self, registry: RightsRegistry):
        with pytest.raises(ValueError, match="name must not be empty"):
            registry.register_asset(type="code", source="oss", name="", license="MIT")


class TestCheckAsset:
    def test_check_existing_asset(self, registry: RightsRegistry):
        aid = registry.register_asset(
            type="code", source="oss", name="test", license="MIT"
        )
        asset = registry.check_asset(aid)
        assert asset["asset_id"] == aid
        assert asset["commercial_ok"] is True
        assert asset["attribution_required"] is True
        assert asset["copyleft"] is False

    def test_check_nonexistent_asset(self, registry: RightsRegistry):
        with pytest.raises(KeyError, match="Asset not found"):
            registry.check_asset("nonexistent-id")

    def test_check_copyleft_asset(self, registry: RightsRegistry):
        aid = registry.register_asset(
            type="code", source="oss", name="gpl-lib", license="GPL-3.0"
        )
        asset = registry.check_asset(aid)
        assert asset["copyleft"] is True
        assert asset["commercial_ok"] is True  # GPL allows commercial

    def test_check_no_commercial(self, registry: RightsRegistry):
        aid = registry.register_asset(
            type="image",
            source="third_party",
            name="restricted-img",
            license="CC-BY-NC",
            restrictions=["no_commercial"],
        )
        asset = registry.check_asset(aid)
        assert asset["commercial_ok"] is False

    def test_check_custom_license(self, registry: RightsRegistry):
        aid = registry.register_asset(
            type="model",
            source="oss",
            name="custom-model",
            license="custom",
            restrictions=["verify_before_commercial"],
        )
        asset = registry.check_asset(aid)
        assert asset["commercial_ok"] is False


class TestListAssets:
    def test_list_empty(self, registry: RightsRegistry):
        assets = registry.list_assets()
        assert assets == []

    def test_list_all(self, registry: RightsRegistry):
        registry.register_asset(type="code", source="oss", name="a", license="MIT")
        registry.register_asset(type="model", source="oss", name="b", license="MIT")
        assets = registry.list_assets()
        assert len(assets) == 2

    def test_list_filtered_by_type(self, registry: RightsRegistry):
        registry.register_asset(type="code", source="oss", name="a", license="MIT")
        registry.register_asset(type="model", source="oss", name="b", license="MIT")
        registry.register_asset(type="code", source="oss", name="c", license="MIT")
        code_assets = registry.list_assets(type="code")
        assert len(code_assets) == 2
        assert all(a["type"] == "code" for a in code_assets)

    def test_list_invalid_type(self, registry: RightsRegistry):
        with pytest.raises(ValueError, match="Invalid type"):
            registry.list_assets(type="invalid")


class TestExportCSV:
    def test_export_empty(self, registry: RightsRegistry):
        csv_str = registry.export_csv()
        assert csv_str == ""

    def test_export_has_header_and_rows(self, registry: RightsRegistry):
        registry.register_asset(type="code", source="oss", name="lib1", license="MIT")
        registry.register_asset(type="model", source="oss", name="m1", license="Apache-2.0")
        csv_str = registry.export_csv()
        lines = csv_str.strip().split("\n")
        assert len(lines) == 3  # header + 2 rows
        assert "asset_id" in lines[0]
        assert "name" in lines[0]
        assert "license" in lines[0]


class TestSeedData:
    def test_seed_creates_six_assets(self, registry: RightsRegistry):
        ids = registry.seed_initial_data()
        assert len(ids) == 6
        assets = registry.list_assets()
        assert len(assets) == 6

    def test_seed_idempotent(self, registry: RightsRegistry):
        ids1 = registry.seed_initial_data()
        ids2 = registry.seed_initial_data()
        assert len(ids1) == 6
        assert len(ids2) == 0  # all skipped
        assets = registry.list_assets()
        assert len(assets) == 6

    def test_seed_comfyui_is_copyleft(self, seeded_registry: RightsRegistry):
        assets = seeded_registry.list_assets()
        comfy = [a for a in assets if a["name"] == "ComfyUI"][0]
        checked = seeded_registry.check_asset(comfy["asset_id"])
        assert checked["copyleft"] is True
        assert checked["commercial_ok"] is True
        assert "derivative_same_license" in checked["restrictions"]

    def test_seed_see_through_needs_verification(self, seeded_registry: RightsRegistry):
        assets = seeded_registry.list_assets()
        st = [a for a in assets if a["name"] == "See-Through"][0]
        checked = seeded_registry.check_asset(st["asset_id"])
        assert checked["commercial_ok"] is False
        assert "license_unconfirmed" in checked["restrictions"]

    def test_seed_ollama_is_permissive(self, seeded_registry: RightsRegistry):
        assets = seeded_registry.list_assets()
        ollama = [a for a in assets if a["name"] == "Ollama"][0]
        checked = seeded_registry.check_asset(ollama["asset_id"])
        assert checked["commercial_ok"] is True
        assert checked["attribution_required"] is True
        assert checked["copyleft"] is False


class TestDBPath:
    def test_custom_db_path(self, tmp_path: Path):
        db = tmp_path / "subdir" / "custom.db"
        reg = RightsRegistry(db_path=db)
        assert db.exists()
        reg.register_asset(type="code", source="oss", name="test", license="MIT")
        assets = reg.list_assets()
        assert len(assets) == 1
        reg.close()
