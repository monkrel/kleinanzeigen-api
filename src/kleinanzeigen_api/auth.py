"""Login handling for the parts of the API that need a real account
(chat, your own ads, posting).

Kleinanzeigen uses Auth0 for login. We do the normal OAuth login (Authorization
Code + PKCE, no client secret) and get back an access token. That token goes on
every logged-in request. The public/search endpoints don't need any of this.

Login is a one-time copy/paste: the app only allows its own redirect URLs, not
localhost, so we can't catch the redirect automatically. You open the login page,
sign in, and paste back the URL you land on (it has the ?code= in it). After that
we keep the refresh token and get new access tokens on our own, so you only log
in once (unless the token gets revoked).

Headless servers / no browser: you don't need a browser on the same machine. Two
ways to do it:
  - Log in once on any machine that has a browser, then copy the saved
    token.json over to the server (or point KLEINANZEIGEN_TOKEN_DIR at it). From
    then on it just refreshes silently - no browser, no prompts.
  - Or set KLEINANZEIGEN_REFRESH_TOKEN to a refresh token you already have (handy
    for CI / containers, where it can come from a secret). No file needed.

Note: automating a logged-in account is against Kleinanzeigen's terms of service
and can get the account banned. Keep it personal and low-volume.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests as creq

# Auth0 settings the app ships with (these are public, not secrets).
AUTH0_DOMAIN = "login.kleinanzeigen.de"
CLIENT_ID = "uV5j90myVPc2XzEOFuWUD2At17OACEGQ"
# we use the https redirect (a real page) so the browser actually lands somewhere
# we can read the ?code= from.
REDIRECT_URI = "https://login.kleinanzeigen.de/android/com.ebay.kleinanzeigen/callback"
SCOPE = "openid email profile offline_access"
AUTHORIZE_URL = f"https://{AUTH0_DOMAIN}/authorize"
TOKEN_URL = f"https://{AUTH0_DOMAIN}/oauth/token"

DEFAULT_TOKEN_PATH = os.path.join(
    os.environ.get("KLEINANZEIGEN_TOKEN_DIR")
    or os.path.join(os.path.expanduser("~"), ".kleinanzeigen_api"),
    "token.json",
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _make_pkce() -> tuple:
    """Make the PKCE verifier + challenge pair for the login."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _jwt_claims(token: str) -> dict:
    """Read the payload out of a JWT. We only need the email, so we don't
    bother checking the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # base64 needs the padding back
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


class NotLoggedIn(RuntimeError):
    """Raised when you call something that needs a login but there isn't one."""


class Authenticator:
    """Keeps the login tokens and gives you a valid access token when asked

    Usage:

        auth = Authenticator()
        if not auth.logged_in:
            auth.login_interactive()      # log in once
        api = KleinanzeigenAPI(authenticator=auth)
        api.conversations()

    Where the tokens are stored
      - by default, ``~/.kleinanzeigen_api/token.json`` (on Windows that's
      - pass ``token_path=...`` to put the file somewhere else
      - or set the ``KLEINANZEIGEN_TOKEN_DIR`` env var to change just the folder

    The refresh token in that file is basically a password, so keep it private
    """

    def __init__(self, token_path: str = DEFAULT_TOKEN_PATH,
                 session: Optional[creq.Session] = None):
        self.token_path = token_path
        self._s = session or creq.Session(impersonate="chrome")
        self._t: dict = self._load()

    # -- persistence -------------------------------------------------------- #
    def _load(self) -> dict:
        try:
            with open(self.token_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
        # For headless / CI: let a refresh token come in through an env
        # The first call then refreshes it into a real token
        rt = os.environ.get("KLEINANZEIGEN_REFRESH_TOKEN")
        if rt:
            return {"refresh_token": rt}
        return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.token_path) or ".", exist_ok=True)
        tmp = self.token_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._t, fh)
        os.replace(tmp, self.token_path)
        try:
            os.chmod(self.token_path, 0o600)  # best-effort; ignored on Windows
        except OSError:
            pass

    # -- state -------------------------------------------------------------- #
    @property
    def logged_in(self) -> bool:
        return bool(self._t.get("refresh_token"))

    @property
    def email(self) -> Optional[str]:
        return self._t.get("email")

    def logout(self) -> None:
        """Forget the stored tokens (does not revoke them server-side)."""
        self._t = {}
        try:
            os.remove(self.token_path)
        except OSError:
            pass

    # -- interactive login -------------------------------------------------- #
    def build_login_url(self) -> tuple:
        """Return (authorize_url, code_verifier, state) to start a login."""
        verifier, challenge = _make_pkce()
        state = _b64url(secrets.token_bytes(16))
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "prompt": "login",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}", verifier, state

    def complete_login(self, redirect_url: str, code_verifier: str,
                       expected_state: Optional[str] = None) -> None:
        """Finish login from the URL the browser landed on after sign-in."""
        qs = parse_qs(urlparse(redirect_url.strip()).query)
        if "error" in qs:
            raise RuntimeError(
                f"Auth0 returned an error: {qs.get('error', [''])[0]} "
                f"{qs.get('error_description', [''])[0]}"
            )
        code = (qs.get("code") or [None])[0]
        if not code:
            raise ValueError(
                "No ?code= found in that URL. Paste the full URL the browser "
                "ended up on after you finished signing in."
            )
        if expected_state and (qs.get("state") or [None])[0] != expected_state:
            raise RuntimeError("state mismatch — login aborted (possible CSRF).")
        self._exchange({
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": REDIRECT_URI,
        })

    def login_interactive(self, open_browser: bool = True) -> None:
        """Run the full one-time login at a prompt (opens browser, asks for the URL)."""
        url, verifier, state = self.build_login_url()
        print("Open this URL and sign in to Kleinanzeigen:\n")
        print(url + "\n")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        print("After signing in the browser will try to open a 'ka-login://' / "
              "kleinanzeigen.de URL (it may show an error page — that's fine).")
        redirect = input("Paste the FULL URL from the address bar here: ").strip()
        self.complete_login(redirect, verifier, state)
        who = f" as {self.email}" if self.email else ""
        print(f"Logged in{who}. Tokens saved to {self.token_path}")

    # -- token use ---------------------------------------------------------- #
    def access_token(self) -> str:
        """Give back a valid access token, refreshing it first if it's old."""
        if not self.logged_in:
            raise NotLoggedIn(
                "Not logged in. Run Authenticator().login_interactive() once."
            )
        # refresh a minute early so we never send an almost-expired token
        if time.time() >= self._t.get("expires_at", 0) - 60:
            self._refresh()
        return self._t["access_token"]

    def _refresh(self) -> None:
        self._exchange({
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": self._t["refresh_token"],
        })

    def _exchange(self, payload: dict) -> None:
        r = self._s.post(TOKEN_URL, json=payload,
                         headers={"Accept": "application/json",
                                  "User-Agent": "Kleinanzeigen Android 2026.25.0"},
                         timeout=30)
        if r.status_code != 200:
            raise RuntimeError(
                f"Auth0 token endpoint returned {r.status_code}: {r.text[:300]}"
            )
        data = r.json()
        self._t["access_token"] = data["access_token"]
        self._t["expires_at"] = time.time() + int(data.get("expires_in", 600))
        # sometimes we get a new refresh token back; if so, keep it
        if data.get("refresh_token"):
            self._t["refresh_token"] = data["refresh_token"]
        idt = data.get("id_token")
        if idt:
            claims = _jwt_claims(idt)
            if claims.get("email"):
                self._t["email"] = claims["email"]
        self._save()
