from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse


DEFAULT_SCOPE = "local-ops"
MAX_REGISTERED_CLIENTS = 50
REFRESH_TOKEN_TTL_SECONDS = 180 * 24 * 60 * 60
# A rotated refresh token presented again within this window is treated as a concurrent retry;
# later reuse revokes the whole grant (OAuth 2.1 refresh-token rotation for public clients).
REFRESH_TOKEN_REUSE_GRACE_SECONDS = 60
# Rotated records are kept this long only to detect reuse, then pruned.
ROTATED_REFRESH_TOKEN_RETENTION_SECONDS = 7 * 24 * 60 * 60
OFFLINE_ACCESS_SCOPE = "offline_access"
GRANT_TYPES = ("authorization_code", "refresh_token")


@dataclass(frozen=True)
class OAuthRuntimeConfig:
    auth_mode: str
    auth_token: str
    public_base_url: str
    state_dir: Path
    oauth_login_token: str
    oauth_scopes: tuple[str, ...]
    oauth_token_ttl_seconds: int
    oauth_refresh_token_ttl_seconds: int = REFRESH_TOKEN_TTL_SECONDS

    @property
    def normalized_auth_mode(self) -> str:
        mode = self.auth_mode.strip().lower()
        if mode:
            return mode
        return "shared_token" if self.auth_token else "none"

    @property
    def login_token(self) -> str:
        return self.oauth_login_token or self.auth_token

    @property
    def scopes(self) -> tuple[str, ...]:
        return self.oauth_scopes or (DEFAULT_SCOPE,)


class OAuthManager:
    def __init__(self, config: OAuthRuntimeConfig, *, mcp_path: str) -> None:
        self.config = config
        self.mcp_path = mcp_path
        self._public_base_url = (
            _validated_public_base_url(config.public_base_url)
            if config.normalized_auth_mode == "oauth"
            else config.public_base_url.strip().rstrip("/")
        )
        self.store_path = config.state_dir / "oauth.json"

    def metadata_base_url(self, fallback_base_url: str) -> str:
        return self._public_base_url or fallback_base_url.rstrip("/")

    def resource_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}{self.mcp_path}"

    def resource_metadata_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/.well-known/oauth-protected-resource{self.mcp_path}"

    def scope_string(self) -> str:
        return " ".join(self.config.scopes)

    def authorization_server_metadata(self, base_url: str) -> dict[str, Any]:
        return {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "registration_endpoint": f"{base_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": list(GRANT_TYPES),
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": list(self.config.scopes),
        }

    def protected_resource_metadata(self, base_url: str) -> dict[str, Any]:
        return {
            "resource": self.resource_url(base_url),
            "authorization_servers": [base_url],
            "scopes_supported": list(self.config.scopes),
            "bearer_methods_supported": ["header"],
            "resource_name": "chatgpt-web-oauth-mcp",
        }

    def register_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise ValueError("redirect_uris must be a non-empty list")
        if not all(isinstance(uri, str) and _is_allowed_redirect_uri(uri) for uri in redirect_uris):
            raise ValueError("redirect_uris must use https or localhost")

        store = self._read_store()
        if len(store["clients"]) >= MAX_REGISTERED_CLIENTS:
            raise ValueError(
                f"too many registered OAuth clients (limit {MAX_REGISTERED_CLIENTS}); "
                "remove unused entries from oauth.json or raise the limit"
            )

        client_id = "mcp_client_" + secrets.token_urlsafe(24)
        now = int(time.time())
        store["clients"][client_id] = {
            "client_id": client_id,
            "client_name": str(payload.get("client_name") or "ChatGPT"),
            "redirect_uris": redirect_uris,
            "created_at": now,
        }
        self._write_store(store)
        return {
            "client_id": client_id,
            "client_name": store["clients"][client_id]["client_name"],
            "redirect_uris": redirect_uris,
            "grant_types": list(GRANT_TYPES),
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_id_issued_at": now,
        }

    def authorize(self, payload: dict[str, str], *, base_url: str) -> str:
        if not self.config.login_token:
            raise PermissionError("oauth login token is not configured")
        if not hmac.compare_digest(payload.get("login_token", ""), self.config.login_token):
            raise PermissionError("invalid login token")

        client_id = payload.get("client_id", "")
        redirect_uri = payload.get("redirect_uri", "")
        code_challenge = payload.get("code_challenge", "")
        if payload.get("response_type") != "code":
            raise ValueError("response_type must be code")
        if payload.get("code_challenge_method") != "S256" or not code_challenge:
            raise ValueError("PKCE S256 is required")
        if payload.get("resource") != self.resource_url(base_url):
            raise ValueError("resource does not match this MCP server")

        store = self._read_store()
        client = store["clients"].get(client_id)
        if not client:
            raise ValueError("unknown client_id")
        if redirect_uri not in client.get("redirect_uris", []):
            raise ValueError("redirect_uri is not registered")

        requested_scopes = _scope_set(payload.get("scope", "")) - {OFFLINE_ACCESS_SCOPE}
        if requested_scopes and not requested_scopes.issubset(set(self.config.scopes)):
            raise ValueError("requested scope is not supported")

        code = "mcp_code_" + secrets.token_urlsafe(32)
        store["codes"][code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scope": _without_offline_access(payload.get("scope", "")) or self.scope_string(),
            "resource": payload.get("resource"),
            "expires_at": int(time.time()) + 300,
        }
        self._write_store(store)

        query = {"code": code}
        if payload.get("state"):
            query["state"] = payload["state"]
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(query)}"

    def exchange_code(self, payload: dict[str, str], *, base_url: str) -> dict[str, Any]:
        if payload.get("grant_type") == "refresh_token":
            return self.refresh_access_token(payload, base_url=base_url)
        if payload.get("grant_type") != "authorization_code":
            raise ValueError("grant_type must be authorization_code or refresh_token")
        if payload.get("resource") != self.resource_url(base_url):
            raise ValueError("resource does not match this MCP server")

        code = payload.get("code", "")
        store = self._read_store()
        code_record = store["codes"].pop(code, None)
        if not code_record:
            self._write_store(store)
            raise ValueError("invalid authorization code")
        if int(code_record.get("expires_at", 0)) < int(time.time()):
            self._write_store(store)
            raise ValueError("authorization code expired")
        if payload.get("client_id") != code_record.get("client_id"):
            self._write_store(store)
            raise ValueError("client_id mismatch")
        if payload.get("redirect_uri") != code_record.get("redirect_uri"):
            self._write_store(store)
            raise ValueError("redirect_uri mismatch")

        verifier = payload.get("code_verifier", "")
        expected_challenge = _pkce_s256(verifier)
        if not hmac.compare_digest(expected_challenge, str(code_record.get("code_challenge", ""))):
            self._write_store(store)
            raise ValueError("invalid code_verifier")

        access_token = "mcp_at_" + secrets.token_urlsafe(40)
        expires_in = max(int(self.config.oauth_token_ttl_seconds), 60)
        store["tokens"][access_token] = {
            "client_id": payload.get("client_id"),
            "scope": code_record.get("scope") or self.scope_string(),
            "resource": code_record.get("resource"),
            "expires_at": int(time.time()) + expires_in,
        }
        refresh_token = self._issue_refresh_token(
            store,
            client_id=str(payload.get("client_id") or ""),
            scope=store["tokens"][access_token]["scope"],
            resource=code_record.get("resource"),
            now=int(time.time()),
        )
        self._write_store(store)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": store["tokens"][access_token]["scope"],
            "refresh_token": refresh_token,
        }

    def refresh_access_token(self, payload: dict[str, str], *, base_url: str) -> dict[str, Any]:
        """Exchange a refresh token for a new access token and a rotated refresh token."""
        raw_token = payload.get("refresh_token", "")
        if not raw_token:
            raise ValueError("refresh_token is required")
        resource = self.resource_url(base_url)
        if payload.get("resource") and payload.get("resource") != resource:
            raise ValueError("resource does not match this MCP server")

        store = self._read_store()
        now = int(time.time())
        pruned = _prune_refresh_tokens(store, now)
        record = store["refresh_tokens"].get(_token_hash(raw_token))
        if not record:
            if pruned:
                self._write_store(store)
            raise ValueError("invalid or expired refresh token")
        client_id = str(record.get("client_id") or "")
        if payload.get("client_id") != client_id:
            raise ValueError("client_id mismatch")
        if record.get("resource") != resource:
            raise ValueError("refresh token was issued for another resource")
        if client_id not in store["clients"]:
            raise ValueError("unknown client_id")
        rotated_at = record.get("rotated_at")
        if rotated_at is not None and now - int(rotated_at) > REFRESH_TOKEN_REUSE_GRACE_SECONDS:
            family_id = record.get("family_id")
            for key, value in list(store["refresh_tokens"].items()):
                if isinstance(value, dict) and value.get("family_id") == family_id:
                    store["refresh_tokens"].pop(key, None)
            self._write_store(store)
            raise ValueError("refresh token reuse detected; the grant was revoked")

        granted = str(record.get("scope") or self.scope_string())
        requested = _scope_set(payload.get("scope", "")) - {OFFLINE_ACCESS_SCOPE}
        if requested and not requested.issubset(_scope_set(granted)):
            raise ValueError("requested scope exceeds the original grant")
        scope = " ".join(sorted(requested)) if requested else granted
        if rotated_at is None:
            record["rotated_at"] = now
            record["expires_at"] = min(
                int(record.get("expires_at") or now),
                now + ROTATED_REFRESH_TOKEN_RETENTION_SECONDS,
            )

        access_token = "mcp_at_" + secrets.token_urlsafe(40)
        expires_in = max(int(self.config.oauth_token_ttl_seconds), 60)
        store["tokens"][access_token] = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "expires_at": now + expires_in,
        }
        refresh_token = self._issue_refresh_token(
            store,
            client_id=client_id,
            scope=granted,
            resource=resource,
            now=now,
            family_id=record.get("family_id"),
        )
        self._write_store(store)
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "scope": scope,
            "refresh_token": refresh_token,
        }

    def _issue_refresh_token(
        self,
        store: dict[str, Any],
        *,
        client_id: str,
        scope: str,
        resource: Any,
        now: int,
        family_id: Any = None,
    ) -> str:
        """Store only the hash; the expiry slides forward on every rotation."""
        _prune_refresh_tokens(store, now)
        token = "mcp_rt_" + secrets.token_urlsafe(48)
        store["refresh_tokens"][_token_hash(token)] = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "family_id": family_id or "rtf_" + secrets.token_urlsafe(16),
            "created_at": now,
            "expires_at": now + max(int(self.config.oauth_refresh_token_ttl_seconds), 60),
        }
        return token

    def verify_access_token(self, token: str, *, base_url: str) -> bool:
        if not token:
            return False
        store = self._read_store()
        record = store["tokens"].get(token)
        if not record:
            return False
        if int(record.get("expires_at", 0)) < int(time.time()):
            store["tokens"].pop(token, None)
            self._write_store(store)
            return False
        if record.get("resource") != self.resource_url(base_url):
            return False
        return _scope_set(str(record.get("scope", ""))).issuperset(set(self.config.scopes))

    def authorize_page(self, payload: dict[str, str]) -> str:
        hidden_inputs = "\n".join(
            f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
            for key, value in payload.items()
        )
        return f"""
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Authorize chatgpt-web-oauth-mcp</title></head>
  <body>
    <main style="font-family: system-ui; max-width: 480px; margin: 48px auto;">
      <h1>Authorize local MCP access</h1>
      <p>Enter your local ops token to let this ChatGPT session call the MCP server.</p>
      <form id="oauth-authorize-form" method="post" action="/oauth/authorize">
        {hidden_inputs}
        <label>Token <input id="oauth-login-token" name="login_token" type="password" autocomplete="current-password" autofocus></label>
        <button type="submit">Authorize</button>
      </form>
    </main>
    <script>
      (function () {{
        var storageKey = "chatgpt-web-oauth-mcp.oauth.login_token";
        var requiredFields = [
          "client_id",
          "redirect_uri",
          "response_type",
          "resource",
          "code_challenge",
          "code_challenge_method"
        ];
        var form = document.getElementById("oauth-authorize-form");
        var tokenInput = document.getElementById("oauth-login-token");

        function readStoredToken() {{
          try {{
            return window.localStorage.getItem(storageKey) || "";
          }} catch (error) {{
            return "";
          }}
        }}

        function saveToken(token) {{
          try {{
            if (token) {{
              window.localStorage.setItem(storageKey, token);
            }}
          }} catch (error) {{
            // Some browser privacy modes block localStorage; normal form auth still works.
          }}
        }}

        function hasCompleteOAuthRequest() {{
          return requiredFields.every(function (name) {{
            return Boolean(form.querySelector('input[name="' + name + '"]'));
          }});
        }}

        if (!form || !tokenInput) {{
          return;
        }}

        form.addEventListener("submit", function () {{
          saveToken(tokenInput.value);
        }});

        var storedToken = readStoredToken();
        if (storedToken && !tokenInput.value) {{
          tokenInput.value = storedToken;
        }}
        if (storedToken && hasCompleteOAuthRequest()) {{
          window.setTimeout(function () {{
            if (form.requestSubmit) {{
              form.requestSubmit();
            }} else {{
              form.submit();
            }}
          }}, 0);
        }}
      }})();
    </script>
  </body>
</html>
""".strip()

    def _read_store(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return {"clients": {}, "codes": {}, "tokens": {}, "refresh_tokens": {}}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"clients": {}, "codes": {}, "tokens": {}, "refresh_tokens": {}}
        if not isinstance(data, dict):
            return {"clients": {}, "codes": {}, "tokens": {}, "refresh_tokens": {}}
        return {
            "clients": data.get("clients") if isinstance(data.get("clients"), dict) else {},
            "codes": data.get("codes") if isinstance(data.get("codes"), dict) else {},
            "tokens": data.get("tokens") if isinstance(data.get("tokens"), dict) else {},
            "refresh_tokens": (
                data.get("refresh_tokens") if isinstance(data.get("refresh_tokens"), dict) else {}
            ),
        }

    def _write_store(self, store: dict[str, Any]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.store_path.parent.chmod(0o700)
        except OSError:
            pass
        tmp_path = self.store_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        try:
            tmp_path.chmod(0o600)
        except OSError:
            pass
        tmp_path.replace(self.store_path)


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _scope_set(scope: str) -> set[str]:
    return {item for item in scope.split() if item}


def _without_offline_access(scope: str) -> str:
    return " ".join(item for item in (scope or "").split() if item and item != OFFLINE_ACCESS_SCOPE)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _prune_refresh_tokens(store: dict[str, Any], now: int) -> bool:
    changed = False
    for key, value in list(store["refresh_tokens"].items()):
        try:
            expired = int(value.get("expires_at", 0)) < now
        except (AttributeError, TypeError, ValueError):
            expired = True
        if expired:
            store["refresh_tokens"].pop(key, None)
            changed = True
    return changed


def _validated_public_base_url(value: str) -> str:
    raw = value or ""
    normalized = raw.strip()
    if not normalized:
        raise ValueError(
            "CHATGPT_MCP_PUBLIC_BASE_URL is required when "
            "CHATGPT_MCP_AUTH_MODE=oauth."
        )
    if (
        normalized != raw
        or "\\" in normalized
        or "#" in normalized
        or "?" in normalized
    ):
        raise ValueError("CHATGPT_MCP_PUBLIC_BASE_URL must be a canonical origin URL.")
    if any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in normalized
    ):
        raise ValueError("CHATGPT_MCP_PUBLIC_BASE_URL must be a canonical origin URL.")
    try:
        parsed = urlparse(normalized)
        parsed.port
    except ValueError:
        raise ValueError("CHATGPT_MCP_PUBLIC_BASE_URL is not a valid URL.") from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("CHATGPT_MCP_PUBLIC_BASE_URL must be a canonical origin URL.")
    hostname = parsed.hostname.lower()
    if parsed.scheme == "https":
        return normalized.rstrip("/")
    if parsed.scheme == "http" and hostname in {"127.0.0.1", "::1", "localhost"}:
        return normalized.rstrip("/")
    raise ValueError(
        "CHATGPT_MCP_PUBLIC_BASE_URL must use https, except for loopback http."
    )


def _is_allowed_redirect_uri(uri: str) -> bool:
    if not uri or uri != uri.strip():
        return False
    if "\\" in uri or "#" in uri:
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in uri):
        return False
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if parsed.fragment or parsed.username is not None or parsed.password is not None:
        return False
    if not parsed.hostname:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname.lower() in {
        "127.0.0.1",
        "::1",
        "localhost",
    }
