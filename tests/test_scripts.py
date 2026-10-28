"""The pipeline scripts: generated docs are fresh, validation fails loudly, reconciliation works."""
from __future__ import annotations

import shutil
import subprocess
import sys

import yaml

from governed_metrics import overlay
from governed_metrics.policy import load_policy

from .conftest import EXAMPLES, ROOT


def run(script, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                          capture_output=True, text=True, cwd=ROOT, check=False)


def test_generated_docs_are_fresh():
    """Stale documentation is a test failure, not a matter of diligence (ADR 0012)."""
    r = run("gen_docs.py", "--check")
    assert r.returncode == 0, r.stderr


def test_example_knowledge_and_policy_validate():
    r = run("validate_knowledge.py")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("ok: 5 metrics, 2 units")


def test_policy_warnings_fail_validation(tmp_path):
    bad = tmp_path / "access.yaml"
    bad.write_text("users:\n  a@example.com:\n    units: [unit_a, unit_typo]\n")
    r = run("validate_knowledge.py", "--policy", str(bad))
    assert r.returncode == 1 and "unit_typo" in r.stderr


def test_reconcile_writes_grants_and_reports_manual_revokes(tmp_path, kb):
    shutil.copy(EXAMPLES / "access.yaml", tmp_path / "access.yaml")
    shutil.copy(EXAMPLES / "access.grants.yaml", tmp_path / "access.grants.yaml")
    ov = str(tmp_path / "overlay.jsonl")
    overlay.grant(ov, "new@example.com", ["unit_b"], by="admin@example.com", reason="joined")
    overlay.revoke(ov, "lead@example.com", by="admin@example.com", reason="left")

    dry = run("reconcile_overlay.py", "--policy", str(tmp_path / "access.yaml"), "--overlay", ov)
    assert "add new@example.com: unit_b" in dry.stdout and "MANUAL: remove lead@example.com" in dry.stdout
    assert yaml.safe_load((tmp_path / "access.grants.yaml").read_text())["users"] == {}

    run("reconcile_overlay.py", "--policy", str(tmp_path / "access.yaml"), "--overlay", ov, "--write")
    users = yaml.safe_load((tmp_path / "access.grants.yaml").read_text())["users"]
    assert users["new@example.com"]["units"] == ["unit_b"]
    assert users["new@example.com"]["granted_by"] == "admin@example.com"

    # After the change deploys, version control governs the user and the overlay is retired.
    p = load_policy(str(tmp_path / "access.yaml"), kb, overlay_path=ov)
    assert p.units_for("new@example.com") == {"unit_b"} and p.grant("new@example.com").origin == "access.grants.yaml"
    assert "new@example.com" in p.reconcile
