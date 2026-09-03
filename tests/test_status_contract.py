"""Unit tests for build_status_dict - the pure mapping from a DriftDecision (or
an error reason) to the status.json contract dict. No Azure, no network, no
clock; mirrors the discipline in test_drift_logic.py."""

from datetime import datetime, timezone

from function_app import DriftDecision, DriftedProperty, build_status_dict

NOW = datetime(2026, 9, 4, 7, 0, 0, tzinfo=timezone.utc)
TS = "2026-09-04T07:00:00Z"

EMPTY_DETAIL = {
    "drifted_count": 0,
    "drifted": [],
    "suppressed_by_cooldown": False,
    "target_missing": False,
}


def _assert_fixed_fields(d):
    assert d["schema_version"] == 1
    assert d["project"] == "azure-drift-detector"
    assert d["cadence"] == "scheduled-daily"
    assert d["repo_url"] == "https://github.com/realitylvn/azure-drift-detector"
    assert d["generated_at"] == TS
    assert d["last_run_at"] == TS


def test_in_sync_is_ok_with_empty_detail():
    d = build_status_dict(DriftDecision("in_sync"), NOW)
    _assert_fixed_fields(d)
    assert d["status"] == "ok"
    assert d["headline"].startswith("In sync")
    assert d["detail"] == EMPTY_DETAIL


def test_single_drift_is_a_finding_and_renders_bool_values_as_strings():
    decision = DriftDecision(
        "drift",
        (DriftedProperty("properties.allowBlobPublicAccess", False, True),),
    )
    d = build_status_dict(decision, NOW)
    assert d["status"] == "finding"
    assert d["headline"] == "1 property drifted"
    assert d["detail"]["drifted_count"] == 1
    assert d["detail"]["drifted"][0] == {
        "property": "properties.allowBlobPublicAccess",
        "expected": "false",
        "actual": "true",
    }
    assert d["detail"]["suppressed_by_cooldown"] is False
    assert d["detail"]["target_missing"] is False


def test_multiple_drift_pluralizes_and_passes_absent_value_through_as_null():
    decision = DriftDecision(
        "drift",
        (
            DriftedProperty("sku.name", "Standard_LRS", "Standard_GRS"),
            DriftedProperty("tags.project", "drift-detector", None),
        ),
    )
    d = build_status_dict(decision, NOW)
    assert d["headline"] == "2 properties drifted"
    assert d["detail"]["drifted"][1]["actual"] is None


def test_suppressed_is_a_finding_with_the_cooldown_flag_set():
    decision = DriftDecision(
        "suppressed",
        (DriftedProperty("sku.name", "Standard_LRS", "Standard_GRS"),),
    )
    d = build_status_dict(decision, NOW)
    assert d["status"] == "finding"
    assert "cooldown" in d["headline"]
    assert d["detail"]["suppressed_by_cooldown"] is True


def test_target_missing_is_an_error_with_the_flag_set():
    d = build_status_dict(DriftDecision("target_missing"), NOW)
    assert d["status"] == "error"
    assert d["headline"] == "Reference target not found"
    assert d["detail"]["target_missing"] is True


def test_error_reason_is_an_error_with_empty_detail():
    d = build_status_dict(None, NOW, error_reason="Azure API call failed")
    _assert_fixed_fields(d)
    assert d["status"] == "error"
    assert d["headline"] == "Azure API call failed"
    assert d["detail"] == EMPTY_DETAIL


def test_result_is_json_serializable():
    import json

    decision = DriftDecision(
        "drift", (DriftedProperty("tags.project", "drift-detector", None),)
    )
    json.dumps(build_status_dict(decision, NOW))
