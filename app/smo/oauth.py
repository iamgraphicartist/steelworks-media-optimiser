"""OAuth 2 for Figma and Dropbox, both with PKCE (S256).

The browser is sent to the provider; the provider redirects back to
http://localhost:<port>/oauth/<provider>/callback, which the bridge server
handles by calling `Provider.finish(code, state)`.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import requests

from .config import Config
from .secure_store import SecureStore


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OAuthError(Exception):
    pass


@dataclass
class _Pending:
    verifier: str
    created: float = field(default_factory=time.time)


class Provider:
    name = ""
    authorize_url = ""
    token_url = ""
    scopes = ""

    def __init__(self, cfg: Config, store: SecureStore, port: int):
        self.cfg, self.store, self.port = cfg, store, port
        self._pending: dict[str, _Pending] = {}

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.port}/oauth/{self.name}/callback"

    # subclasses supply client credentials
    def client_id(self) -> Optional[str]:
        raise NotImplementedError

    def configured(self) -> bool:
        return bool(self.client_id())

    def extra_auth_params(self) -> dict:
        return {}

    def start(self) -> str:
        cid = self.client_id()
        if not cid:
            raise OAuthError(f"No {self.name.title()} app key yet. Add it in Settings (see README, 'Register the OAuth apps').")
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        self._pending = {k: v for k, v in self._pending.items() if time.time() - v.created < 600}
        self._pending[state] = _Pending(verifier)
        q = {"client_id": cid, "redirect_uri": self.redirect_uri, "response_type": "code", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        if self.scopes:
            q["scope"] = self.scopes
        q.update(self.extra_auth_params())
        return self.authorize_url + "?" + urllib.parse.urlencode(q)

    def finish(self, code: str, state: str) -> dict:
        p = self._pending.pop(state, None)
        if not p:
            raise OAuthError("This sign-in link has expired. Please try again from the app.")
        tok = self._exchange(code, p.verifier)
        tok["obtained_at"] = time.time()
        self.store.set(f"{self.name}_token", tok)
        return tok

    def _exchange(self, code: str, verifier: str) -> dict:
        raise NotImplementedError

    def _refresh(self, tok: dict) -> dict:
        raise NotImplementedError

    def token(self) -> Optional[dict]:
        return self.store.get(f"{self.name}_token")

    def access_token(self) -> Optional[str]:
        tok = self.token()
        if not tok:
            return None
        exp = tok.get("expires_in")
        if exp and time.time() > tok.get("obtained_at", 0) + int(exp) - 120 and tok.get("refresh_token"):
            try:
                new = self._refresh(tok)
                new.setdefault("refresh_token", tok["refresh_token"])
                new["obtained_at"] = time.time()
                for k in ("account", "user"):
                    if k in tok:
                        new[k] = tok[k]
                self.store.set(f"{self.name}_token", new)
                tok = new
            except Exception:
                return None
        return tok.get("access_token")

    def sign_out(self) -> None:
        self.store.delete(f"{self.name}_token")

    @staticmethod
    def _check(r: requests.Response) -> dict:
        if r.status_code >= 400:
            try:
                msg = r.json()
            except ValueError:
                msg = r.text[:300]
            raise OAuthError(f"Sign-in was refused ({r.status_code}): {msg}")
        return r.json()


class FigmaOAuth(Provider):
    name = "figma"
    authorize_url = "https://www.figma.com/oauth"
    token_url = "https://api.figma.com/v1/oauth/token"
    refresh_url = "https://api.figma.com/v1/oauth/refresh"
    scopes = "current_user:read file_content:read file_metadata:read"

    def client_id(self):
        return self.cfg.get("figma_client_id")

    def client_secret(self):
        return self.cfg.get("figma_client_secret") or self.store.get("figma_client_secret")

    def configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def _post(self, url: str, data: dict) -> dict:
        cid, secret = self.client_id(), self.client_secret()
        if not secret:
            raise OAuthError("The Figma client secret is missing. Add it in Settings.")
        # Current Figma API: client credentials as HTTP Basic auth.
        r = requests.post(url, data=data, auth=(cid, secret), timeout=30)
        if r.status_code in (400, 401):  # older endpoint shape: credentials in the body
            r = requests.post(url, data={**data, "client_id": cid, "client_secret": secret}, timeout=30)
        return self._check(r)

    def _exchange(self, code, verifier):
        return self._post(self.token_url, {"redirect_uri": self.redirect_uri, "code": code,
                                           "grant_type": "authorization_code", "code_verifier": verifier})

    def _refresh(self, tok):
        return self._post(self.refresh_url, {"refresh_token": tok["refresh_token"]})


class DropboxOAuth(Provider):
    name = "dropbox"
    authorize_url = "https://www.dropbox.com/oauth2/authorize"
    token_url = "https://api.dropboxapi.com/oauth2/token"
    scopes = "account_info.read files.metadata.read files.content.read sharing.read"

    def client_id(self):
        return self.cfg.get("dropbox_app_key")

    def extra_auth_params(self):
        return {"token_access_type": "offline"}

    def _exchange(self, code, verifier):
        r = requests.post(self.token_url, data={"code": code, "grant_type": "authorization_code",
                                                "client_id": self.client_id(), "redirect_uri": self.redirect_uri,
                                                "code_verifier": verifier}, timeout=30)
        return self._check(r)

    def _refresh(self, tok):
        r = requests.post(self.token_url, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                                "client_id": self.client_id()}, timeout=30)
        return self._check(r)
