"""Unit tests for the pure drift-decision logic in function_app.evaluate_drift.
Deterministic: no Azure, no network, no clock. The function takes two property
dicts plus config and returns a DriftDecision; all I/O stays in the entrypoint."""

import copy
from datetime import datetime, timedelta, timezone

from function_app import WATCHED_PATHS, evaluate_drift

NOW = datetime(2026, 9, 2, 7, 0, tzinfo=timezone.utc)

INTENDED = {
    "sku": {"name": "Standard_LRS"},
    "tags": {
        "portfolio": "azure-devops-portfolio",
        "project": "drift-detector",
        "environment": "dev",
    },
    "properties": {
        "minimumTlsVersion": "TLS1_2",
        "allowBlobPublicAccess": False,
        "supportsHttpsTrafficOnly": True,
    },
}


def _copy(d):
    return copy.deepcopy(d)


def _decide(actual, *, last_alert_utc=None, cooldown_days=3, now=NOW):
    return evaluate_drift(
        INTENDED,
        actual,
        watched_paths=WATCHED_PATHS,
        last_alert_utc=last_alert_utc,
        now=now,
        cooldown_days=cooldown_days,
    )


def test_in_sync_when_every_watched_property_matches():
    decision = _decide(_copy(INTENDED))
    assert decision.outcome == "in_sync"
    assert decision.drifted == ()


def test_drift_on_a_single_scalar_property():
    actual = _copy(INTENDED)
    actual["sku"]["name"] = "Standard_GRS"
    decision = _decide(actual)
    assert decision.outcome == "drift"
    assert len(decision.drifted) == 1
    d = decision.drifted[0]
    assert (d.path, d.expected, d.actual) == ("sku.name", "Standard_LRS", "Standard_GRS")


def test_drift_lists_every_changed_property():
    actual = _copy(INTENDED)
    actual["properties"]["allowBlobPublicAccess"] = True
    actual["properties"]["minimumTlsVersion"] = "TLS1_1"
    decision = _decide(actual)
    assert decision.outcome == "drift"
    assert {d.path for d in decision.drifted} == {
        "properties.allowBlobPublicAccess",
        "properties.minimumTlsVersion",
    }


def test_drift_when_a_watched_tag_is_removed():
    actual = _copy(INTENDED)
    del actual["tags"]["project"]
    decision = _decide(actual)
    assert decision.outcome == "drift"
    d = next(d for d in decision.drifted if d.path == "tags.project")
    assert d.expected == "drift-detector"
    assert d.actual is None


def test_drift_when_a_watched_tag_value_changes():
    actual = _copy(INTENDED)
    actual["tags"]["environment"] = "prod"
    decision = _decide(actual)
    assert decision.outcome == "drift"
    assert any(d.path == "tags.environment" for d in decision.drifted)


def test_unwatched_extra_tag_is_ignored():
    actual = _copy(INTENDED)
    actual["tags"]["costCenter"] = "12345"
    decision = _decide(actual)
    assert decision.outcome == "in_sync"


def test_drift_is_suppressed_inside_the_cooldown_window():
    actual = _copy(INTENDED)
    actual["sku"]["name"] = "Standard_GRS"
    decision = _decide(actual, last_alert_utc=NOW - timedelta(days=1), cooldown_days=3)
    assert decision.outcome == "suppressed"
    assert len(decision.drifted) == 1


def test_drift_alerts_again_once_cooldown_has_expired():
    actual = _copy(INTENDED)
    actual["sku"]["name"] = "Standard_GRS"
    decision = _decide(actual, last_alert_utc=NOW - timedelta(days=5), cooldown_days=3)
    assert decision.outcome == "drift"


def test_target_missing_when_actual_is_none():
    decision = _decide(None)
    assert decision.outcome == "target_missing"
    assert decision.drifted == ()
