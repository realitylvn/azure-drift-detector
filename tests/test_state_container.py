"""The dedupe-state blob must be reached with an account-key connection string,
NOT the managed identity - the identity has no data-plane role on the detector's
own storage account, so an identity-based client 403s on every run (caught in
first deploy: `AuthorizationPermissionMismatch` from Windows-Azure-Blob)."""

import function_app


def test_state_container_uses_the_connection_string_not_a_credential(monkeypatch):
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
    monkeypatch.setenv("STATE_STORAGE_CONNECTION_STRING", "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=k;EndpointSuffix=core.windows.net")
    monkeypatch.setenv("STATE_CONTAINER_NAME", "state")

    result = function_app._state_container()

    assert result == "container-client"
    assert captured["conn_str"].startswith("DefaultEndpointsProtocol=")
    assert "AccountKey=" in captured["conn_str"]
    assert captured["container"] == "state"


def test_state_container_takes_no_credential_argument():
    import inspect

    sig = inspect.signature(function_app._state_container)
    assert list(sig.parameters) == []
