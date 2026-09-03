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


def test_web_container_uses_the_connection_string_and_the_web_container(monkeypatch):
    import function_app

    captured = {}

    class FakeBlobService:
        @classmethod
        def from_connection_string(cls, conn_str):
            captured["conn_str"] = conn_str
            return cls()

        def get_container_client(self, name):
            captured["container"] = name
            return "container-client"

    monkeypatch.setattr(function_app, "BlobServiceClient", FakeBlobService)
    monkeypatch.setenv(
        "STATE_STORAGE_CONNECTION_STRING",
        "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=k;EndpointSuffix=core.windows.net",
    )

    result = function_app._web_container()

    assert result == "container-client"
    assert "AccountKey=" in captured["conn_str"]
    assert captured["container"] == "$web"


def test_publish_status_swallows_a_storage_failure(monkeypatch):
    import function_app

    def boom():
        raise RuntimeError("storage unreachable")

    monkeypatch.setattr(function_app, "_web_container", boom)

    # Must not raise - a publish failure can never fail the run.
    function_app._publish_status({"schema_version": 1})


def test_publish_status_uploads_status_json_as_json(monkeypatch):
    import function_app

    calls = {}

    class FakeContainer:
        def upload_blob(self, name, data, overwrite, content_settings):
            calls["name"] = name
            calls["data"] = data
            calls["overwrite"] = overwrite
            calls["content_type"] = content_settings.content_type

    monkeypatch.setattr(function_app, "_web_container", lambda: FakeContainer())

    function_app._publish_status({"schema_version": 1, "status": "ok"})

    assert calls["name"] == "status.json"
    assert calls["overwrite"] is True
    assert calls["content_type"] == "application/json"
    assert '"status": "ok"' in calls["data"]
