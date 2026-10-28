from __future__ import annotations

import json
import os
import stat
from datetime import date, datetime, timezone

import pytest

from governed_metrics import overlay
from governed_metrics.audit import AuditLog, rows_band
from governed_metrics.errors import InvalidRequest
from governed_metrics.rules import list_rules, resolve_rule

# ---- overlay -------------------------------------------------------------------------------

def test_overlay_is_owner_only_and_last_event_wins(tmp_path):
    path = str(tmp_path / "state" / "overlay.jsonl")
    assert overlay.grant(path, "A@Example.com", ["unit_a"], by="admin", reason="r")
    assert overlay.revoke(path, "a@example.com", by="admin", reason="r")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert overlay.effective(path)["a@example.com"]["action"] == "revoke"
    overlay.mark_reconciled(path, ["a@example.com"])
    assert overlay.effective(path) == {}


def test_overlay_ignores_malformed_lines_and_events(tmp_path):
    path = tmp_path / "o.jsonl"
    path.write_text('not json\n{"action": "explode", "user": "x"}\n{"action": "grant"}\n'
                    '{"action": "grant", "user": "b@example.com", "units": ["unit_a"]}\n')
    assert list(overlay.effective(str(path))) == ["b@example.com"]
    assert not overlay.append(str(path), {"action": "nope", "user": "x"})
    assert not overlay.append("", {"action": "grant", "user": "x"})


def test_overlay_signature_changes_on_write(tmp_path):
    path = str(tmp_path / "o.jsonl")
    assert overlay.signature(path) is None
    overlay.grant(path, "c@example.com", ["unit_a"], by="admin", reason="r")
    first = overlay.signature(path)
    overlay.grant(path, "d@example.com", ["unit_a"], by="admin", reason="r")
    assert overlay.signature(path) != first


# ---- rules -----------------------------------------------------------------------------------

@pytest.mark.parametrize("unit,as_of,value,layer,effective", [
    ("unit_a", date(2025, 6, 1), 50, "group_default", "2025-01-01"),
    ("unit_a", date(2026, 3, 1), 60, "group_default", "2026-03-01"),
    ("unit_b", date(2026, 6, 1), 40, "unit_override", "2025-06-01"),
    ("unit_b", date(2025, 3, 1), 50, "group_default", "2025-01-01"),   # before the override existed
])
def test_rules_resolve_most_specific_then_latest_in_force(kb, unit, as_of, value, layer, effective):
    r = resolve_rule(kb, "shipping", "free_shipping_threshold", unit, as_of)
    assert (r["value"], r["layer"], r["effective"]) == (value, layer, effective)


def test_an_unverified_entry_in_force_is_refused_not_skipped(kb):
    assert resolve_rule(kb, "shipping", "returns_window_days", "unit_a", date(2026, 8, 31))["value"] == 30
    with pytest.raises(InvalidRequest) as e:
        resolve_rule(kb, "shipping", "returns_window_days", "unit_a", date(2026, 9, 1))
    assert e.value.reason == "rule_unverified"


@pytest.mark.parametrize("domain,key,as_of,reason", [
    ("shiping", "free_shipping_threshold", date(2026, 1, 1), "unknown_rule"),
    ("shipping", "nope", date(2026, 1, 1), "unknown_rule"),
    ("shipping", "free_shipping_threshold", date(2024, 1, 1), "rule_not_in_force"),
])
def test_unknown_or_not_yet_in_force_rules_are_refused(kb, domain, key, as_of, reason):
    with pytest.raises(InvalidRequest) as e:
        resolve_rule(kb, domain, key, "unit_a", as_of)
    assert e.value.reason == reason


def test_rule_catalogue_lists_no_values(kb):
    catalogue = list_rules(kb)["rules"]["shipping"]
    assert catalogue["free_shipping_threshold"]["unit_overrides"] == ["unit_b"]
    for entry in catalogue.values():
        assert set(entry) == {"description", "unit_overrides", "has_verified_entries"}
    assert not any(str(v) in json.dumps(catalogue) for v in (40, 45, 50, 60))


# ---- audit -----------------------------------------------------------------------------------

def test_audit_records_allow_listed_fields_only(tmp_path):
    log = AuditLog(str(tmp_path / "audit" / "audit.jsonl"))
    log.record(caller="a@example.com", tool="run_query", outcome="ok", sql="select secret",
               rows=[["j@example.com"]], unit="unit_a")
    entry = json.loads((tmp_path / "audit" / "audit.jsonl").read_text())
    assert set(entry) == {"ts", "caller", "tool", "outcome", "unit"}
    assert stat.S_IMODE(os.stat(log.path).st_mode) == 0o600


def test_audit_rotates_monthly(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.record(tool="t", outcome="ok")
    old = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
    os.utime(log.path, (old, old))
    log.record(tool="t", outcome="ok")
    assert [os.path.basename(f) for f in log.files()] == ["audit-2026-01.jsonl", "audit.jsonl"]
    assert len(list(log.read())) == 2


def test_audit_write_failures_are_counted_not_raised(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    log = AuditLog(str(blocker / "audit.jsonl"))
    log.record(tool="t")
    assert log.write_failures == 1


@pytest.mark.parametrize("n,band", [(None, None), (0, "0"), (7, "1-10"), (100, "11-100"),
                                    (1000, "101-1000"), (5000, ">1000")])
def test_rows_are_logged_as_bands(n, band):
    assert rows_band(n) == band


def test_audit_fields_are_length_capped(tmp_path):
    log = AuditLog(str(tmp_path / "audit.jsonl"))
    log.record(tool="get_metric", unit="u" * 10_000, metric="m\n" * 5_000)
    entry = json.loads((tmp_path / "audit.jsonl").read_text())
    assert len(entry["unit"]) <= 200 and len(entry["metric"]) <= 200
