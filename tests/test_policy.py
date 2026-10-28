"""Deny by default; admin separate from data; the overlay is a buffer, not the truth."""
from __future__ import annotations

import textwrap

import pytest

from governed_metrics import overlay
from governed_metrics.policy import load_policy


def write(path, text):
    path.write_text(textwrap.dedent(text))
    return str(path)


@pytest.fixture
def example(kb, policy_dir):
    return load_policy(str(policy_dir / "access.yaml"), kb)


def test_unlisted_and_empty_identities_get_nothing(example):
    assert example.units_for("stranger@example.com") == frozenset()
    assert example.units_for("") == frozenset()
    assert not example.can_read_unit("stranger@example.com", "unit_a")


def test_admin_rights_confer_no_data_access(example):
    assert example.is_admin("admin@example.com")
    assert example.units_for("admin@example.com") == frozenset()
    assert not example.is_admin("lead@example.com")


def test_wildcard_units_expand_to_known_units(example):
    assert example.units_for("lead@example.com") == {"unit_a", "unit_b"}


def test_metric_allow_list_and_dimension_restriction(example):
    assert example.metric_allowed("partner@example.com", "unit_a", "revenue")
    assert not example.metric_allowed("partner@example.com", "unit_a", "refunds")
    assert example.allowed_dimensions("partner@example.com", "revenue") == {"channel"}
    assert example.allowed_dimensions("partner@example.com", "orders") is None
    assert example.metric_allowed("analyst@example.com", "unit_a", "refunds")


def test_adhoc_is_per_unit_and_off_unless_granted(example):
    assert example.adhoc_sources("lead@example.com", "unit_b") == {"unit_b.orders"}
    assert example.adhoc_sources("analyst@example.com", "unit_a") is None
    assert example.unmasked("steward@example.com") and not example.unmasked("lead@example.com")


@pytest.mark.parametrize("body,error", [
    (None, "not found"),
    ("users: [1, 2]\n", "'users'"),
    ("admins: someone\n", "'admins' must be a list"),
    ("- a\n- b\n", "unreadable"),
    ("users: {a: {units: [unit_a]}}\ninclude: [more.yaml]\n", "more than one place"),
])
def test_structural_problems_deny_everyone(kb, tmp_path, body, error):
    path = tmp_path / "access.yaml"
    if body is not None:
        path.write_text(body)
        (tmp_path / "more.yaml").write_text("users: {a: {units: [unit_b]}}\n")
    p = load_policy(str(path), kb)
    assert not p.ok and error in p.error
    assert p.units_for("a") == frozenset() and not p.is_admin("a")


def test_included_files_may_not_carry_admins(kb, tmp_path):
    write(tmp_path / "access.yaml", "include: [g.yaml]\n")
    write(tmp_path / "g.yaml", "admins: [x@example.com]\n")
    assert not load_policy(str(tmp_path / "access.yaml"), kb).ok


def test_typos_narrow_access_and_are_reported(kb, tmp_path):
    path = write(tmp_path / "access.yaml", """
        users:
          a@example.com:
            units: [unit_a, unit_z]
            metrics: {unit_a: [revenue, revnue]}
            adhoc: {"*": ["*"], unit_b: ["*"]}
            unmasked: "yes"
    """)
    p = load_policy(path, kb)
    assert p.ok and p.units_for("a@example.com") == {"unit_a"}
    assert not p.metric_allowed("a@example.com", "unit_a", "revnue")
    assert p.adhoc_sources("a@example.com", "unit_a") is None
    assert not p.unmasked("a@example.com")
    assert len(p.warnings) >= 4


def test_overlay_grant_applies_only_to_unlisted_users(kb, policy_dir):
    path = str(policy_dir / "overlay.jsonl")
    overlay.grant(path, "new@example.com", ["unit_b"], by="admin@example.com", reason="joined team")
    overlay.grant(path, "analyst@example.com", ["unit_b"], by="admin@example.com", reason="should not widen")
    p = load_policy(str(policy_dir / "access.yaml"), kb, overlay_path=path)
    assert p.units_for("new@example.com") == {"unit_b"}
    assert p.grant("new@example.com").adhoc is None and p.grant("new@example.com").origin == "overlay"
    assert p.units_for("analyst@example.com") == {"unit_a"}           # version control wins
    assert p.reconcile == ("analyst@example.com",)


def test_overlay_revoke_overrides_version_control(kb, policy_dir):
    path = str(policy_dir / "overlay.jsonl")
    overlay.revoke(path, "lead@example.com", by="admin@example.com", reason="left")
    p = load_policy(str(policy_dir / "access.yaml"), kb, overlay_path=path)
    assert p.units_for("lead@example.com") == frozenset()
    assert "lead@example.com" in p.revoked and p.reconcile == ()


def test_revoking_an_overlay_only_user_is_reconciled_at_once(kb, policy_dir):
    path = str(policy_dir / "overlay.jsonl")
    overlay.grant(path, "temp@example.com", ["unit_a"], by="admin@example.com", reason="cover")
    overlay.revoke(path, "temp@example.com", by="admin@example.com", reason="done")
    p = load_policy(str(policy_dir / "access.yaml"), kb, overlay_path=path)
    assert p.units_for("temp@example.com") == frozenset()
    assert p.reconcile == ("temp@example.com",)


def test_overlay_never_grants_data_access_to_an_admin(kb, policy_dir):
    """An admin's data access comes only from reviewed version control: anyone who can fix
    access must not be able to quietly take it (access-model.md)."""
    path = str(policy_dir / "overlay.jsonl")
    overlay.grant(path, "admin@example.com", ["unit_a"], by="admin@example.com", reason="self")
    p = load_policy(str(policy_dir / "access.yaml"), kb, overlay_path=path)
    assert p.units_for("admin@example.com") == frozenset()
    assert "admin@example.com" in p.reconcile
