import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.mgmt.storage import StorageManagementClient
from azure.storage.blob import BlobServiceClient, ContentSettings

# The azure-identity / azure-*-storage SDKs log every HTTP request and response at
# INFO, which buries this function's own one-line decision trace (the thing the
# Log Alert matches on, and the thing a human reads in App Insights). Quiet them
# to WARNING - real failures still surface, the routine request dumps don't.
logging.getLogger("azure").setLevel(logging.WARNING)

WATCHED_PATHS = [
    "sku.name",
    "tags.portfolio",
    "tags.project",
    "tags.environment",
    "properties.minimumTlsVersion",
    "properties.allowBlobPublicAccess",
    "properties.supportsHttpsTrafficOnly",
]

STATE_BLOB_NAME = "last-alert.json"
_REFERENCE_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "reference_template.json")

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
    plain strings. NB: the SDK attribute enable_https_traffic_only maps to the
    ARM property supportsHttpsTrafficOnly."""
    return {
        "sku": {"name": str(account.sku.name)},
        "tags": dict(account.tags or {}),
        "properties": {
            "minimumTlsVersion": str(account.minimum_tls_version),
            "allowBlobPublicAccess": bool(account.allow_blob_public_access),
            "supportsHttpsTrafficOnly": bool(account.enable_https_traffic_only),
        },
    }


def _load_reference_template():
    with open(_REFERENCE_TEMPLATE_PATH) as fh:
        return json.load(fh)


def _get_storage_account(credential, subscription_id, resource_group, account_name):
    client = StorageManagementClient(credential, subscription_id)
    return client.storage_accounts.get_properties(resource_group, account_name)


def _state_container():
    """Container client for the dedupe-state blob, over an account-key connection
    string - NOT the managed identity. The identity holds only Reader on the
    reference resource group; it has no data-plane role on this storage account,
    and granting one just to write a single timestamp would widen it for no
    reason. STATE_STORAGE_CONNECTION_STRING is wired in resources.bicep from the
    same account key as AzureWebJobsStorage."""
    container_name = os.environ["STATE_CONTAINER_NAME"]
    blob_service = BlobServiceClient.from_connection_string(
        os.environ["STATE_STORAGE_CONNECTION_STRING"]
    )
    return blob_service.get_container_client(container_name)


STATUS_BLOB_NAME = "status.json"
WEB_CONTAINER_NAME = "$web"


def _web_container():
    """Container client for the public $web blob, over the same account-key
    connection string the dedupe-state blob uses - NOT the managed identity,
    which has no data-plane role here. Static-website hosting is turned on out
    of band by scripts/enable-static-website.ps1 (an azd postprovision hook),
    so $web serves status.json anonymously without allowBlobPublicAccess."""
    blob_service = BlobServiceClient.from_connection_string(
        os.environ["STATE_STORAGE_CONNECTION_STRING"]
    )
    return blob_service.get_container_client(WEB_CONTAINER_NAME)


def _publish_status(status_dict) -> None:
    """Best-effort publish of status.json to $web. A failure here must never
    fail the run - same guard as _set_last_alert_time. The dashboard treats a
    missing or stale file as 'unreachable', which is the honest outcome."""
    try:
        _web_container().upload_blob(
            STATUS_BLOB_NAME,
            json.dumps(status_dict, indent=2),
            overwrite=True,
            content_settings=ContentSettings(
                content_type="application/json", cache_control="max-age=300"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning(f"Could not publish status.json: {exc}")


def _get_last_alert_time(container):
    blob = container.get_blob_client(STATE_BLOB_NAME)
    if not blob.exists():
        return None
    data = json.loads(blob.download_blob().readall())
    return datetime.fromisoformat(data["last_alert_utc"])


def _read_last_alert_time(container):
    """_get_last_alert_time, but a storage failure returns None instead of raising.
    The dedupe timestamp is best-effort: if we can't read it, worst case is one
    duplicate email, which beats crashing a run that might need to alert."""
    try:
        return _get_last_alert_time(container)
    except Exception as exc:  # noqa: BLE001
        logging.warning(f"Could not read dedupe state, treating as no prior alert: {exc}")
        return None


def _set_last_alert_time(container, when: datetime) -> None:
    blob = container.get_blob_client(STATE_BLOB_NAME)
    blob.upload_blob(json.dumps({"last_alert_utc": when.isoformat()}), overwrite=True)


def _fmt(value) -> str:
    """Render a property value for the drift trace: match Azure/portal wording for
    bools, mark an absent value explicitly."""
    if value is None:
        return "(absent)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


SCHEMA_VERSION = 1
PROJECT_SLUG = "azure-drift-detector"
REPO_URL = "https://github.com/realitylvn/azure-drift-detector"


def _status_value(v):
    """Render a drifted-property value for status.json: a bool as the lowercase
    string Azure/the portal uses, an absent value (None) as JSON null, anything
    else stringified."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _drift_status(decision):
    """(status, headline, detail) for a completed DriftDecision."""
    drifted = [
        {
            "property": d.path,
            "expected": _status_value(d.expected),
            "actual": _status_value(d.actual),
        }
        for d in decision.drifted
    ]
    n = len(drifted)
    unit = "property" if n == 1 else "properties"
    detail = {
        "drifted_count": n,
        "drifted": drifted,
        "suppressed_by_cooldown": decision.outcome == "suppressed",
        "target_missing": decision.outcome == "target_missing",
    }
    if decision.outcome == "in_sync":
        return "ok", "In sync - 0 drifted properties", detail
    if decision.outcome == "target_missing":
        return "error", "Reference target not found", detail
    if decision.outcome == "suppressed":
        return "finding", f"{n} {unit} drifted - alert in cooldown", detail
    return "finding", f"{n} {unit} drifted", detail


def build_status_dict(decision, now, *, error_reason=None):
    """Pure: a DriftDecision (or an error_reason string) plus a clock value in,
    the status.json contract dict out. No I/O, no clock, no globals - all of
    that stays in the entrypoint, same split as evaluate_drift."""
    if error_reason is not None:
        status, headline, detail = "error", error_reason, {
            "drifted_count": 0,
            "drifted": [],
            "suppressed_by_cooldown": False,
            "target_missing": False,
        }
    else:
        status, headline, detail = _drift_status(decision)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": SCHEMA_VERSION,
        "project": PROJECT_SLUG,
        "cadence": "scheduled-daily",
        "generated_at": ts,
        "last_run_at": ts,
        "status": status,
        "headline": headline,
        "detail": detail,
        "repo_url": REPO_URL,
    }


app = func.FunctionApp()


@app.timer_trigger(schedule="0 0 7 * * *", arg_name="timer", run_on_startup=False)
def drift_check(timer: func.TimerRequest) -> None:
    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    target_rg = os.environ["TARGET_RESOURCE_GROUP"]
    target_account = os.environ["TARGET_STORAGE_ACCOUNT_NAME"]
    cooldown_days = int(os.environ.get("ALERT_COOLDOWN_DAYS", "3"))

    credential = DefaultAzureCredential()

    try:
        account = _get_storage_account(
            credential, subscription_id, target_rg, target_account
        )
    except ResourceNotFoundError:
        # The reference RG or storage account is genuinely gone. Distinct setup
        # error - not drift. evaluate_drift returns "target_missing" for actual=None.
        account = None
    except AzureError as exc:
        logging.error(f"Azure API call failed, skipping this run: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - nothing here may crash the app
        logging.error(f"Unexpected error reading the target, skipping this run: {exc}")
        return

    expected = build_expected(_load_reference_template())
    actual = build_actual(account) if account is not None else None

    now = datetime.now(timezone.utc)
    container = _state_container()
    last_alert = _read_last_alert_time(container)

    decision = evaluate_drift(
        expected,
        actual,
        watched_paths=WATCHED_PATHS,
        last_alert_utc=last_alert,
        now=now,
        cooldown_days=cooldown_days,
    )

    if decision.outcome == "in_sync":
        logging.info("Reference target in sync (0 drifted properties).")
    elif decision.outcome == "target_missing":
        # This "DriftDetectorSetupError:" prefix is what infra/resources.bicep's
        # setup-error scheduledQueryRules alert matches on - keep them in sync.
        logging.error(
            f"DriftDetectorSetupError: storage account {target_account} not found "
            f"in resource group {target_rg}"
        )
    elif decision.outcome == "suppressed":
        n = len(decision.drifted)
        logging.info(
            f"Drift still present ({n} propert{'y' if n == 1 else 'ies'}) but "
            f"suppressed - last alert was within the {cooldown_days}-day cooldown."
        )
    elif decision.outcome == "drift":
        n = len(decision.drifted)
        summary = "; ".join(
            f"{d.path} expected {_fmt(d.expected)} got {_fmt(d.actual)}"
            for d in decision.drifted
        )
        # This "DriftDetected:" prefix is what infra/resources.bicep's drift
        # scheduledQueryRules alert matches on - keep them in sync.
        logging.warning(
            f"DriftDetected: {n} propert{'y' if n == 1 else 'ies'} drifted - {summary}"
        )
        # The alert has already been raised via the trace above. A failure to
        # persist the cooldown timestamp must not fail the run - worst case is a
        # duplicate email on the next run, which beats a crash after we've alerted.
        try:
            _set_last_alert_time(container, now)
        except Exception as exc:  # noqa: BLE001
            logging.warning(f"Could not persist dedupe timestamp: {exc}")
