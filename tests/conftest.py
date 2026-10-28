from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from governed_metrics.config import Settings
from governed_metrics.knowledge.loader import load_knowledge
from governed_metrics.runtime import Runtime

from .fakes import TODAY, FakeWarehouse

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

# A fixed knowledge version keeps cache keys and assertions independent of git state.
os.environ.setdefault("GM_KNOWLEDGE_VERSION", "test")


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        knowledge_path=str(EXAMPLES / "knowledge"), policy_path=str(tmp_path / "access.yaml"),
        overlay_path=str(tmp_path / "state" / "overlay.jsonl"),
        audit_path=str(tmp_path / "state" / "audit.jsonl"), transport="stdio", host="127.0.0.1",
        port=8080, public_url="http://127.0.0.1:8080", local_user="lead@example.com",
        statement_timeout_ms=30_000, adhoc_row_cap_default=200, adhoc_row_cap_max=2_000,
        breakdown_row_cap=1_000, cache_ttl_s=0, freshness_ttl_s=0, adhoc_rate_per_minute=60,
        max_parallel_legs=8, auth_mode="none", jwt=None)
    return replace(base, **overrides)


@pytest.fixture(scope="session")
def kb():
    return load_knowledge(str(EXAMPLES / "knowledge"))


@pytest.fixture
def policy_dir(tmp_path: Path) -> Path:
    shutil.copy(EXAMPLES / "access.yaml", tmp_path / "access.yaml")
    shutil.copy(EXAMPLES / "access.grants.yaml", tmp_path / "access.grants.yaml")
    return tmp_path


@pytest.fixture
def warehouse() -> FakeWarehouse:
    return FakeWarehouse()


@pytest.fixture
def settings(policy_dir: Path) -> Settings:
    return make_settings(policy_dir)


@pytest.fixture
def rt(settings: Settings, warehouse: FakeWarehouse, kb) -> Runtime:
    return Runtime(settings, warehouse, knowledge=kb, today=lambda: TODAY)
