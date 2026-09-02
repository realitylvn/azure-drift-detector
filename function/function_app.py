import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import azure.functions as func

WATCHED_PATHS = [
    "sku.name",
    "tags.portfolio",
    "tags.project",
    "tags.environment",
    "properties.minimumTlsVersion",
    "properties.allowBlobPublicAccess",
    "properties.supportsHttpsTrafficOnly",
]

_MISSING = object()


@dataclass(frozen=True)
class DriftedProperty:
    path: str
    expected: object
    actual: object


@dataclass(frozen=True)
class DriftDecision:
    """Outcome of comparing live storage-account state against the reference template.

    outcome is one of: "in_sync", "drift", "suppressed", "target_missing".
    drifted holds one DriftedProperty per changed path (populated for "drift"
    and "suppressed", empty otherwise).
    """

    outcome: str
    drifted: tuple = ()


def _dig(mapping, dotted_path):
    """Walk a dotted path through nested dicts. Returns _MISSING if any segment
    is absent or a non-dict is hit before the end."""
    current = mapping
    for segment in dotted_path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current


def evaluate_drift(
    expected,
    actual,
    *,
    watched_paths,
    last_alert_utc,
    now,
    cooldown_days,
) -> DriftDecision:
    """Decide whether the live resource has drifted from intended state, and
    whether this run should alert. Pure: no I/O, no clock, no globals.

    actual is None when the target resource could not be read at all - that's a
    setup error ("target_missing"), deliberately not treated as drift.
    """
    if actual is None:
        return DriftDecision("target_missing")

    drifted = []
    for path in watched_paths:
        exp = _dig(expected, path)
        act = _dig(actual, path)
        if exp != act:
            drifted.append(
                DriftedProperty(
                    path,
                    None if exp is _MISSING else exp,
                    None if act is _MISSING else act,
                )
            )

    if not drifted:
        return DriftDecision("in_sync")

    drifted = tuple(drifted)
    if last_alert_utc is not None and (now - last_alert_utc) < timedelta(days=cooldown_days):
        return DriftDecision("suppressed", drifted)
    return DriftDecision("drift", drifted)


def build_expected(template_json) -> dict:
    """Pull the watched properties of the storage account out of a compiled ARM
    template. Handles both the symbolic-name object form and the legacy array
    form of the `resources` node."""
    resources = template_json["resources"]
    items = resources.values() if isinstance(resources, dict) else resources
    storage = next(
        r for r in items if r.get("type") == "Microsoft.Storage/storageAccounts"
    )
    props = storage["properties"]
    return {
        "sku": {"name": storage["sku"]["name"]},
        "tags": dict(storage.get("tags", {})),
        "properties": {
            "minimumTlsVersion": props["minimumTlsVersion"],
            "allowBlobPublicAccess": props["allowBlobPublicAccess"],
            "supportsHttpsTrafficOnly": props["supportsHttpsTrafficOnly"],
        },
    }


def build_actual(account) -> dict:
    """Shape an azure-mgmt-storage StorageAccount model into the same dict form
    as build_expected. str() the SDK enums so log messages and comparisons see
    plain strings."""
    return {
        "sku": {"name": str(account.sku.name)},
        "tags": dict(account.tags or {}),
        "properties": {
            "minimumTlsVersion": str(account.minimum_tls_version),
            "allowBlobPublicAccess": bool(account.allow_blob_public_access),
            "supportsHttpsTrafficOnly": bool(account.enable_https_traffic_only),
        },
    }


app = func.FunctionApp()


@app.timer_trigger(schedule="0 0 7 * * *", arg_name="timer", run_on_startup=False)
def drift_check(timer: func.TimerRequest) -> None:
    """Placeholder - fleshed out in Task 5."""
