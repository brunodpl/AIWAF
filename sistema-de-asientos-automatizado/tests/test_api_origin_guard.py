from fastapi.testclient import TestClient

from src.api.main import app


client = TestClient(app)


def test_rejects_requests_from_untrusted_browser_origins():
    response = client.get("/health", headers={"Origin": "https://attacker.example"})

    assert response.status_code == 403


def test_allows_the_local_ui_origin():
    response = client.get("/health", headers={"Origin": "http://localhost:3003"})

    assert response.status_code == 200


def test_allows_non_browser_health_checks_without_origin():
    response = client.get("/health")

    assert response.status_code == 200
