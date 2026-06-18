"""Offline tests for the Auth0 login helper. No real network is used - the
token endpoint is faked with a small stand-in session."""
import base64
import hashlib
import json
import time

import pytest

from kleinanzeigen_api import Authenticator, NotLoggedIn
from kleinanzeigen_api import auth


def _fake_jwt(payload: dict) -> str:
    """Build a fake JWT (we only ever read the middle part, never verify it)."""
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeSession:
    """Stands in for the curl_cffi session and just hands back canned replies."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append(json)
        return self._responses.pop(0)


def test_pkce_challenge_matches_verifier():
    verifier, challenge = auth._make_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected


def test_jwt_claims_reads_email():
    assert auth._jwt_claims(_fake_jwt({"email": "me@x.de"}))["email"] == "me@x.de"
    assert auth._jwt_claims("not-a-jwt") == {}


def test_login_url_has_pkce_and_client(tmp_path):
    a = Authenticator(token_path=str(tmp_path / "t.json"))
    url, verifier, state = a.build_login_url()
    assert url.startswith(auth.AUTHORIZE_URL)
    assert "code_challenge_method=S256" in url
    assert auth.CLIENT_ID in url
    assert verifier and state


def test_access_token_without_login_raises(tmp_path):
    a = Authenticator(token_path=str(tmp_path / "t.json"))
    assert a.logged_in is False
    with pytest.raises(NotLoggedIn):
        a.access_token()


def test_complete_login_exchanges_and_saves(tmp_path):
    path = str(tmp_path / "t.json")
    sess = FakeSession([FakeResp(200, {
        "access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
        "id_token": _fake_jwt({"email": "me@x.de"})})])
    a = Authenticator(token_path=path, session=sess)
    _, verifier, state = a.build_login_url()
    a.complete_login(f"https://callback/?code=abc&state={state}", verifier, state)

    assert a.logged_in and a.email == "me@x.de"
    assert a.access_token() == "AT1"  # still valid, no second request
    assert len(sess.requests) == 1
    # a fresh object should read the saved tokens back from disk
    again = Authenticator(token_path=path)
    assert again.logged_in and again.email == "me@x.de"


def test_complete_login_rejects_wrong_state(tmp_path):
    a = Authenticator(token_path=str(tmp_path / "t.json"), session=FakeSession([]))
    with pytest.raises(RuntimeError):
        a.complete_login("https://cb/?code=abc&state=WRONG", "verifier", "RIGHT")


def test_complete_login_needs_a_code(tmp_path):
    a = Authenticator(token_path=str(tmp_path / "t.json"), session=FakeSession([]))
    with pytest.raises(ValueError):
        a.complete_login("https://cb/?state=s", "verifier", None)


def test_access_token_refreshes_when_expired(tmp_path):
    a = Authenticator(token_path=str(tmp_path / "t.json"),
                      session=FakeSession([FakeResp(200, {"access_token": "AT2",
                                                          "expires_in": 3600})]))
    # pretend we have an old token that already expired
    a._t = {"access_token": "OLD", "refresh_token": "RT",
            "expires_at": time.time() - 10}
    assert a.access_token() == "AT2"          # it refreshed
    assert a._t["refresh_token"] == "RT"      # and kept the refresh token


def test_refresh_token_from_env_for_headless(tmp_path, monkeypatch):
    # no token file, but a refresh token is provided via env (CI / container case)
    monkeypatch.setenv("KLEINANZEIGEN_REFRESH_TOKEN", "RT_FROM_ENV")
    a = Authenticator(token_path=str(tmp_path / "missing.json"),
                      session=FakeSession([FakeResp(200, {"access_token": "AT3",
                                                          "expires_in": 3600})]))
    assert a.logged_in is True            # seeded from the env var
    assert a.access_token() == "AT3"      # first call refreshes it into a real token


def test_logout_forgets_everything(tmp_path):
    path = str(tmp_path / "t.json")
    a = Authenticator(token_path=path, session=FakeSession([
        FakeResp(200, {"access_token": "AT", "refresh_token": "RT",
                       "expires_in": 3600})]))
    a.complete_login("https://cb/?code=c&state=s", "v", None)
    assert a.logged_in
    a.logout()
    assert a.logged_in is False
