# REVIEW.md — Build Log & Learning Notes

Personal study companion to this project. Unlike `README.md` (public / recruiter-facing),
this file tracks *why* each decision was made and logs every `az` / `azd` command as it
runs, so the reasoning isn't reconstructed from memory afterwards. Written progressively
at each build checkpoint, same as Cost Sentinel and the Offboarding Automator.

Design spec: `docs/superpowers/specs/2026-09-02-drift-detector-design.md`.
Implementation plan: `docs/superpowers/plans/2026-09-02-drift-detector.md`.

---

## What this project is

Catches when someone manually changes something in the Azure portal that no longer
matches what the IaC template says should be there. A daily Azure Function reads one
live storage account, compares seven declared properties against a compiled reference
Bicep template, and on any mismatch logs a `DriftDetected:` trace that a Log Alert turns
into an email — the same trace → alert → Action Group → inbox chain proven in Cost
Sentinel.

Third project in the portfolio. Forks Cost Sentinel's skeleton (`azd` + Python
Consumption Function + subscription-scoped Bicep + GitHub Actions validation) rather
than rebuilding it.

---

## Why this approach

- **Stateless, Bicep-as-source-of-truth.** No stored "last known good" state. The
  reference Bicep template *is* the definition of intended state; it's compiled to
  `function/reference_template.json`, shipped inside the Function, and compared against
  live Azure on every run. Nothing to migrate, back up, or get out of sync.

- **The one deliberate exception (dedupe blob).** Reused unchanged from Cost Sentinel:
  a single `last-alert.json` timestamp blob so a *sustained* drift (nobody fixes the
  manual change for a week) alerts once and then stays quiet for a cooldown window
  instead of emailing every day.

- **Compiled-template comparison, NOT `az deployment group what-if`.** `what-if` is the
  most literal "template vs reality" engine, and it was the obvious first instinct, but
  two things ruled it out for V1:
  1. `what-if` routinely reports spurious `Modify` changes on no-effect properties
     (API-version defaults, properties the RP normalises). The single most persuasive
     part of this repo is a clean before/after demo — "make one portal change, watch
     the detector catch it, redeploy the template, watch it go green." A phantom diff
     on the "green" side would wreck that.
  2. `what-if` needs deployment-action RBAC on the resource group
     (`Microsoft.Resources/deployments/*`), which is far more than "read the storage
     account's properties." The whole least-privilege story here is *one* `Reader`
     assignment.
  So instead: `az bicep build` the reference template at build time, read the live
  resource with `azure-mgmt-storage` (`Reader` is enough), and compare only the
  property paths the template explicitly declares. Bicep stays the genuine source of
  truth — the expected values are compiled from the `.bicep`, never hand-copied — and
  the comparison can't produce a phantom diff because it only looks at paths the
  template names. Recorded here rather than buried because it's the central design
  trade-off.

- **Separate reference deployment + deliberate deploy order.** The watched storage
  account is deployed *by hand* (`az deployment group create`) into its own resource
  group, `rg-drift-detector-reference-dev` — not bundled into the detector's `azd up`.
  Reasons: the watched infra is conceptually independent of the watcher; a "legitimate
  redeploy shows zero drift" test stays a clean one-liner; and `azd` never tries to
  manage or tear down the thing being watched. The order (reference first, then
  `azd up`) is a *deliberate dependency*, not a manual step to remember: the detector's
  `main.bicep` creates a `Reader` role assignment scoped to the reference resource
  group, so that group must exist first.

- **One cross-resource-group `Reader` assignment.** The Function's system-assigned
  managed identity gets exactly one role: built-in `Reader`, scoped to
  `rg-drift-detector-reference-dev` — not the subscription, not the detector's own
  resource group. `get_properties` only reads. This is *tighter* than Cost Sentinel
  (which needed Cost Management Reader). The Functions host's own storage uses an
  account-key connection string (an app setting, encrypted at rest), so no
  `Storage Blob Data *` data-plane roles are needed either.

- **Two Log Alert rules, not one.** `alert-drift-*` fires on a `DriftDetected:` trace
  (severity 3). `alert-setup-*` fires on a `DriftDetectorSetupError:` trace (severity 2
  — higher, the detector itself is broken), which happens if the reference resource
  group or storage account has been deleted out from under it. ~15 lines of extra
  Bicep to close the "detector silently goes dark and nobody notices" failure mode —
  the same Operations/Reliability instinct that caught Cost Sentinel's re-run handling
  and the Offboarding script's false-success bug.

- **Shared storage-account-name derivation.** Both `reference/reference.bicep` and the
  detector's `infra/resources.bicep` compute the reference storage account name from
  the identical expression
  `stddref${substring(uniqueString(subscription().id, 'drift-detector-reference'), 0, 10)}`,
  so the detector can wire `TARGET_STORAGE_ACCOUNT_NAME` without a human copying a
  deployment output between the two deploys.

---

## Decisions & platform quirks hit during the build

### Checkpoint 1 — scaffold, reference template, pure logic (Tasks 1–4)

- **A Bicep `var` compiles to an ARM expression, which broke the literal-values
  requirement.** First cut of `reference.bicep` defined the tag set as
  `var tags = { ... }` and referenced it as `tags: tags` on the resource. `az bicep
  build` hoisted that into a `variables` block and emitted `"tags": "[variables('tags')]"`
  in the compiled JSON — a *string expression*, not a literal object. The Function
  reads the compiled template at runtime to learn the intended tag values; it can't
  evaluate `[variables('tags')]`. Fix: inline the tag object directly on the resource
  (`tags: { portfolio: '...' ... }`). Bicep keeps an all-literal inline object as a
  literal in ARM. The literal-values unit test now also asserts `tags` deserialised to
  a `dict`, not a `str`, so this can't regress silently. General lesson: "every watched
  value must be a literal" means *inline* literal — no `var`, no `param` — on any
  property the runtime comparison reads.

- **The `az`-bundled Bicep here is v0.46.1 and emits the array form of `resources`.**
  Newer Bicep can emit a symbolic-name *object* form (`"resources": { "myStorage": {...} }`).
  `build_expected` handles both (`resources.values()` if it's a dict, else iterate the
  list) so a future Bicep upgrade in CI won't break it.

- **Pinned `azure-mgmt-storage==21.2.1`, deliberately NOT the current 25.x.** The 25.x
  line is a from-scratch rewrite on the new `azure.core.rest` / typespec model where a
  `StorageAccount` exposes a nested `.properties` object
  (`account.properties.minimum_tls_version`) with partial, fiddly backward-compat
  flattening. 21.2.1 keeps the long-stable *flattened* msrest model —
  `account.sku.name`, `account.tags`, `account.minimum_tls_version`,
  `account.allow_blob_public_access`, `account.enable_https_traffic_only` — which is
  what every example and the plan's `build_actual` expects. This is exactly the class
  of "unpinned major bump silently changes the API" problem that cost Cost Sentinel two
  deploy cycles; choosing the stable model up front and pinning it hard is the lesson
  from that project applied.

- **SDK attribute name ≠ ARM property name.** The ARM/Bicep property is
  `supportsHttpsTrafficOnly`; the `azure-mgmt-storage` model attribute is
  `enable_https_traffic_only`. `build_actual` is the single place that mapping lives,
  and it's unit-tested against a fake account so the mapping is pinned.

- **17 unit tests, all pure** — no Azure, no network, no clock. `evaluate_drift` branch
  coverage (in-sync, single-property drift, multi-property drift, watched-tag removed,
  watched-tag changed, unwatched-tag ignored, suppressed-in-cooldown,
  alert-after-cooldown, target-missing) plus the normalisation mapping plus the
  worker-indexes-exactly-`drift_check` check that a plain `azd deploy` never does.

### Checkpoint 2 — timer entrypoint + detector Bicep (Tasks 5–7)

- **The entrypoint is a thin shell around the pure function, same shape as Cost
  Sentinel.** `drift_check` reads six app settings, calls `azure-mgmt-storage`
  `get_properties` once inside a `try`, then hands two dicts to `evaluate_drift` and
  acts on the outcome. All the branching logic that's worth testing lives in the pure
  function; the entrypoint just does I/O and logging.

- **`ResourceNotFoundError` is caught and turned into `actual=None`, not an early
  return.** That routes a genuinely-deleted reference resource through the same
  decision path as everything else and comes out as `target_missing` → a
  `DriftDetectorSetupError:` trace → the setup-error alert. Any *other* Azure error
  (throttling, transient 5xx, auth blip) logs and returns without alerting — a missed
  run is harmless, it checks again on the next schedule. This is the "distinct failure
  state, not drift" requirement from the spec, implemented as one code path rather than
  two.

- **`_fmt` renders bools as `true`/`false` in the trace**, matching the Azure portal's
  own wording, so the alert email reads
  `allowBlobPublicAccess expected false got true` — legible to someone who has never
  seen the code.

- **Trace-prefix coupling is called out in both files.** `function_app.py` and
  `infra/resources.bicep` each carry a comment saying the `DriftDetected:` /
  `DriftDetectorSetupError:` strings must stay in sync with the other file. The
  scheduled-query alert matches on `startswith "DriftDetected:"` — the trailing colon
  is deliberate so it can't also match a hypothetical future `DriftDetected…` string
  (`DriftDetect**ed**` and `DriftDetect**or**SetupError` already diverge before the
  colon, but matching it makes the boundary explicit).

- **The cross-resource-group role assignment is a Bicep module, not a manual
  `az role assignment create`.** `main.bicep` is subscription-scoped; it deploys
  `resources.bicep` into `rg-drift-detector-dev` and, separately,
  `reference-rbac.bicep` into `resourceGroup('rg-drift-detector-reference-dev')` —
  a different resource group than the one being created in the same deployment. Bicep
  resolves the second group by name at deploy time, so it must already exist. That's
  what makes "deploy the reference template first" a real dependency the tooling
  enforces, not a checklist item. The Function's `principalId` flows
  `resources.bicep` output → `main.bicep` → module param.

- **`Reader` role GUID verified, not assumed** — `az role definition list --name
  Reader --query "[0].name" -o tsv` → `acdd72a7-3385-48ef-bd42-f606fba81ae7`.
  Hardcoded in `reference-rbac.bicep` with the verification command in a comment.

- **Action Group `groupShortName` is `driftdtct`** — 9 chars, under the hard 12-char
  limit Azure enforces on that field (Cost Sentinel used `costsentnl` for the same
  reason).

- **No Budget resource here.** Cost Sentinel owns the subscription's
  `Microsoft.Consumption/budgets` guardrail; a second one would just be a duplicate.
  This project's cost story is "nothing here costs anything" (Functions free grant,
  an empty storage account, capped Log Analytics).

- **All three Bicep files compile clean** (`az bicep build`, zero warnings):
  `infra/main.bicep`, `infra/resources.bicep`, `infra/reference-rbac.bicep`.
  17 Python tests still green.

---

## CLI command log

| Command | What it did / why |
|---|---|
| `git init` / `git config user.*` | Project-scoped repo inside `azure-drift-detector/`, isolated from any stray `.git` higher up the tree (same setup Cost Sentinel documented). |
| `az account show` | Pre-flight identity check before drafting any Bicep — confirmed **LVN Subscription** (`<SUBSCRIPTION_ID>-…`), tenant `<TENANT_ID>-…`, signed in as `user@contoso.com`. |
| `az functionapp list` / `az vm list-usage -l eastus2` | Confirmed Cost Sentinel's Function App is already running in East US 2, i.e. the `Microsoft.Web` / Y1 Consumption quota that blocked Project 1 is cleared there with headroom — so this project targets the same region. |
| `git checkout -b build/drift-detector-v1` | Feature branch for the implementation work; merges back at the end via the finishing-a-development-branch flow. |
| `py -m venv .venv` + `pip install -r requirements-dev.txt` | Local test environment. `requirements-dev.txt` pulls in `function/requirements.txt` so the unit tests import the same pinned runtime deps that deploy. |
| `az bicep install` | Installed the Bicep CLI (v0.46.1 bundled) so `az bicep build` works. |
| `az bicep build --file reference/reference.bicep --outfile function/reference_template.json` | Compiled the reference "intended state" template to the ARM JSON the Function ships and reads. Run once now; CI re-runs it and fails if the committed copy is stale. First run exposed the `var`→expression issue above. |
| `pip install azure-mgmt-storage==21.2.1` | Downgraded from the auto-picked 25.1.0 after inspecting both models — 21.2.1 has the flattened attributes the code expects (see decision above). |
| `.venv\Scripts\python -m pytest -q` | 17 tests green at checkpoint 1. Runs with no Azure and no clock because all I/O stays in the (still stub) timer entrypoint. |
| `az role definition list --name Reader --query "[0].name" -o tsv` | Verified the built-in `Reader` role GUID (`acdd72a7-3385-48ef-bd42-f606fba81ae7`) before hardcoding it into `infra/reference-rbac.bicep`, rather than guessing. |
| `az bicep build --file infra/resources.bicep --stdout` | Compiled the detector resources template (Function App, storage + `state` container, Log Analytics, App Insights, Y1 plan, Action Group, two scheduled-query alert rules). Clean, zero warnings. |
| `az bicep build --file infra/main.bicep --stdout` | Compiled the whole subscription-scoped tree including the cross-RG `reference-rbac.bicep` module. Clean. |

*(Still no `azd` commands and no resource-creating `az` commands — checkpoints 1–2 are all local: code, Bicep, and `az bicep build`. `azd provision` / `azd deploy` and the reference-group deployment happen at Task 9, behind an explicit go-ahead gate.)*

---

## AZ-900 / AZ-104 domain mapping

Filled in as each area is actually exercised.

- **Governance / IaC as source of truth** *(designed, partially built)* — the entire
  project is a working argument for why configuration drift from IaC matters and how
  you detect it. `infra/` is Bicep through `azd`; the reference template is compiled
  and version-controlled; CI gates every change on `az bicep build`.
- **Least-privilege RBAC** *(built at Task 7)* — a single built-in `Reader`
  assignment, cross-resource-group, scoped to exactly the watched resource group. Good
  concrete contrast with Cost Sentinel's Cost Management Reader and a demonstration of
  scoping a role to a resource group rather than a subscription. The `guid()` name is
  derived from `(resourceGroup().id, principalId, roleId)` so the assignment is
  idempotent across redeploys.
- **Deployment & dependencies** *(built at Task 7)* — the deliberate "reference group
  first, detector second" order, enforced by a cross-RG role-assignment module
  (`scope: resourceGroup(referenceResourceGroupName)` in a subscription-scoped
  `main.bicep`) rather than a written instruction, is hands-on AZ-104 "automate
  deployment of resources" / deployment-dependency material. Module output-to-param
  wiring passes the Function's `principalId` from one module to another.
- **Monitoring** *(built at Task 6)* — Azure Monitor scheduled query (log) alerts
  scoped to the App Insights resource (not the workspace — the `traces` vs `AppTraces`
  table-name distinction), Action Groups with the 12-char short-name limit,
  Application Insights with sampling disabled and Log Analytics ingestion capped at
  1 GB/day. Two alert rules at severity 3 (drift) and severity 2 (detector broken).
- **Security posture** *(designed)* — `allowBlobPublicAccess` is the featured drift
  example: storage-account exposure settings, the same "unauthorised exposure" theme
  the NSG Scanner project picks up one layer down.
- **Service limits & quotas** *(confirmed)* — re-verified the `Microsoft.Web` / Y1
  Consumption quota family is distinct from `Microsoft.Compute` VM quota and already
  cleared in East US 2 from Project 1.
- **SDK / dependency management** *(exercised at checkpoint 1)* — pinning every runtime
  dependency with `==`, and consciously choosing a stable SDK model version over the
  newest release, as a reproducible-build discipline.
