"""Public deployment-target configuration exposed to the owner frontend."""

from fastapi.testclient import TestClient

from xagent.web.app import app


def test_deployment_config_preserves_standalone_client_origins(monkeypatch):
    monkeypatch.setenv(
        "XAGENT_PUBLIC_API_BASE_URL",
        " https://api.example.test/ ",
    )
    monkeypatch.setenv(
        "XAGENT_APP_BASE_URL",
        " https://app.example.test/ ",
    )

    response = TestClient(app).get("/api/deployment-config")

    assert response.status_code == 200
    assert response.json() == {
        # Standalone installations may host API and widget assets separately.
        # Leaving the common override unset preserves each client's existing
        # API-config or browser-origin fallback.
        "deployment_origin": None,
        "app_origin": "https://app.example.test",
        "region": None,
    }
