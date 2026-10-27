import base64
import hashlib

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from windows_mcp.infrastructure import oauth
from windows_mcp.infrastructure.oauth import OAuthStore, build_oauth_routes

CLIENT_ID = "client"
CLIENT_SECRET = "secret"
REDIRECT_URI = "http://127.0.0.1:9999/callback"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)


@pytest.fixture
def store():
    return OAuthStore()


@pytest.fixture
def client(store):
    routes = build_oauth_routes(store, "http://localhost:8000", CLIENT_ID, CLIENT_SECRET)
    app = Starlette(
        routes=[Route(path, handler, methods=methods) for path, (handler, methods) in routes.items()]
    )
    return TestClient(app, follow_redirects=False)


def _authorize(client, redirect_uri=REDIRECT_URI):
    return client.get(
        "/oauth/authorize",
        params={
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )


def _code_from(response) -> str:
    return response.headers["location"].split("code=")[1].split("&")[0]


def test_full_flow_issues_token(client):
    code = _code_from(_authorize(client))
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "Bearer"


def test_authorize_does_not_accumulate_redirect_uris(client, store):
    for port in range(5):
        _authorize(client, f"http://127.0.0.1:{port + 1}/cb")
    assert store.clients[CLIENT_ID].redirect_uris == []


def test_authorize_caps_pending_codes(client, store, monkeypatch):
    monkeypatch.setattr(oauth, "MAX_PENDING_CODES", 3)
    for _ in range(3):
        assert _authorize(client).status_code == 302
    response = _authorize(client)
    assert response.status_code == 503
    assert len(store.codes) == 3


def test_expired_codes_are_pruned(client, store, monkeypatch):
    _authorize(client)
    assert len(store.codes) == 1
    monkeypatch.setattr(oauth.time, "time", lambda: 10**12)
    _authorize(client)
    # The stale code is gone; only the one just issued remains.
    assert len(store.codes) == 1


@pytest.mark.parametrize("body", ["[1, 2]", '"text"', '{"grant_type": 1}', "{not json"])
def test_token_rejects_malformed_json(client, body):
    response = client.post(
        "/oauth/token", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
