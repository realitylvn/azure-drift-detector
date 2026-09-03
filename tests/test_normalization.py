"""build_expected reads the compiled reference template; build_actual shapes the
azure-mgmt-storage model. Both must produce the identical dict form evaluate_drift
compares, so a no-change live account yields zero drift."""

import json
import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace

from function_app import build_actual, build_expected, evaluate_drift, WATCHED_PATHS

REAL_TEMPLATE = json.loads(
    (pathlib.Path(__file__).parent.parent / "function" / "reference_template.json").read_text()
)


def test_build_expected_extracts_the_watched_shape_from_the_real_template():
    expected = build_expected(REAL_TEMPLATE)
    assert expected["sku"]["name"] == "Standard_LRS"
    assert expected["tags"]["project"] == "drift-detector"
    assert expected["properties"]["allowBlobPublicAccess"] is False
    assert expected["properties"]["minimumTlsVersion"] == "TLS1_2"
    assert expected["properties"]["supportsHttpsTrafficOnly"] is True


def _fake_account(**overrides):
    base = dict(
        sku_name="Standard_LRS",
        tags={
            "portfolio": "azure-devops-portfolio",
            "project": "drift-detector",
            "environment": "dev",
        },
        minimum_tls_version="TLS1_2",
        allow_blob_public_access=False,
        enable_https_traffic_only=True,
    )
    base.update(overrides)
    return SimpleNamespace(
        sku=SimpleNamespace(name=base["sku_name"]),
        tags=base["tags"],
        minimum_tls_version=base["minimum_tls_version"],
        allow_blob_public_access=base["allow_blob_public_access"],
        enable_https_traffic_only=base["enable_https_traffic_only"],
    )


def test_build_actual_maps_every_watched_field():
    actual = build_actual(_fake_account())
    assert actual == {
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


def test_build_actual_handles_a_null_tags_collection():
    actual = build_actual(_fake_account(tags=None))
    assert actual["tags"] == {}


def test_real_template_against_a_matching_fake_account_is_in_sync():
    decision = evaluate_drift(
        build_expected(REAL_TEMPLATE),
        build_actual(_fake_account()),
        watched_paths=WATCHED_PATHS,
        last_alert_utc=None,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        cooldown_days=3,
    )
    assert decision.outcome == "in_sync"


def test_public_access_flip_against_the_real_template_is_drift():
    decision = evaluate_drift(
        build_expected(REAL_TEMPLATE),
        build_actual(_fake_account(allow_blob_public_access=True)),
        watched_paths=WATCHED_PATHS,
        last_alert_utc=None,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        cooldown_days=3,
    )
    assert decision.outcome == "drift"
    assert decision.drifted[0].path == "properties.allowBlobPublicAccess"
