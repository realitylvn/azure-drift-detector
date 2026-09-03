"""The shipped reference_template.json is the Function's source of truth for
intended state. If a watched property compiled to an ARM expression instead of a
literal, the runtime comparison would be meaningless - assert it never does."""

import json
import pathlib

TEMPLATE = json.loads(
    (pathlib.Path(__file__).parent.parent / "function" / "reference_template.json").read_text()
)

WATCHED_LITERALS = {
    ("sku", "name"): "Standard_LRS",
    ("properties", "minimumTlsVersion"): "TLS1_2",
    ("properties", "allowBlobPublicAccess"): False,
    ("properties", "supportsHttpsTrafficOnly"): True,
}
WATCHED_TAGS = {
    "portfolio": "azure-devops-portfolio",
    "project": "drift-detector",
    "environment": "dev",
}


def _storage_resource():
    resources = TEMPLATE["resources"]
    items = resources.values() if isinstance(resources, dict) else resources
    return next(r for r in items if r.get("type") == "Microsoft.Storage/storageAccounts")


def test_watched_scalar_properties_are_the_expected_literals():
    s = _storage_resource()
    for (outer, inner), expected in WATCHED_LITERALS.items():
        actual = s[outer][inner]
        assert actual == expected
        assert not (isinstance(actual, str) and actual.startswith("[")), (
            f"{outer}.{inner} compiled to an ARM expression, not a literal"
        )


def test_watched_tags_are_the_expected_literals():
    s = _storage_resource()
    assert isinstance(s["tags"], dict), "tags compiled to an ARM expression, not a literal object"
    for key, expected in WATCHED_TAGS.items():
        assert s["tags"][key] == expected
