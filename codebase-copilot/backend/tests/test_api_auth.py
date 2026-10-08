import os
import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.auth import get_expected_session_token, verify_session_token, resolve_or_create_session_token

client = TestClient(app)

def test_health_endpoint_is_always_public():
    """Health check endpoints must never require authentication so watchdogs remain unimpeded."""
    resp = client.get("/health")
    assert resp.status_code == 200

    resp_api = client.get("/api/health")
    assert resp_api.status_code == 200

def test_unauthenticated_request_blocked_when_token_configured(monkeypatch):
    """When CLAY_SESSION_TOKEN is set in the environment, API requests without token receive 401."""
    test_token = "clay_secret_token_1234567890abcdef"
    monkeypatch.setenv("CLAY_SESSION_TOKEN", test_token)

    resp = client.post("/api/settings", json={"temperature": 0.2})
    assert resp.status_code == 401
    data = resp.json()
    assert "detail" in data or "error" in data

def test_invalid_token_blocked(monkeypatch):
    """Requests with incorrect tokens must receive 401."""
    test_token = "clay_secret_token_1234567890abcdef"
    monkeypatch.setenv("CLAY_SESSION_TOKEN", test_token)

    resp = client.post(
        "/api/settings",
        json={"temperature": 0.2},
        headers={"X-Clay-Auth-Token": "invalid_wrong_token"}
    )
    assert resp.status_code == 401
    data = resp.json()
    assert "detail" in data or "error" in data

def test_valid_token_header_accepted(monkeypatch):
    """Requests with X-Clay-Auth-Token matching CLAY_SESSION_TOKEN pass authentication."""
    test_token = "clay_secret_token_1234567890abcdef"
    monkeypatch.setenv("CLAY_SESSION_TOKEN", test_token)

    resp = client.get(
        "/api/agent/task",
        headers={"X-Clay-Auth-Token": test_token}
    )
    assert resp.status_code == 200

def test_bearer_token_authorization_header_accepted(monkeypatch):
    """Requests with standard Authorization: Bearer <token> header pass authentication."""
    test_token = "clay_secret_token_1234567890abcdef"
    monkeypatch.setenv("CLAY_SESSION_TOKEN", test_token)

    resp = client.get(
        "/api/agent/task",
        headers={"Authorization": f"Bearer {test_token}"}
    )
    assert resp.status_code == 200

def test_verify_session_token_timing_safe():
    """Verify timing-safe token verification function."""
    assert verify_session_token("valid_token_123", "valid_token_123") is True
    assert verify_session_token("valid_token_123", "wrong_token") is False
    assert verify_session_token("", "valid_token_123") is False
    assert verify_session_token("valid_token_123", "") is False

def test_resolve_or_create_session_token_generates_and_persists(tmp_path, monkeypatch):
    """Ensure token resolver creates ~/.clay directory and writes valid JSON file."""
    monkeypatch.delenv("CLAY_SESSION_TOKEN", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    token = resolve_or_create_session_token()
    assert isinstance(token, str)
    assert len(token) >= 32

    # Verify persistent file was written
    auth_file = fake_home / ".clay" / "auth_token.json"
    assert auth_file.exists()

    # Calling resolve again returns the exact same token
    second_token = resolve_or_create_session_token()
    assert second_token == token
