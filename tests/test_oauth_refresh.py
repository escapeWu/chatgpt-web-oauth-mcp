"""OAuth refresh-token grant: issuance, rotation, reuse detection and expiry."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from chatgpt_web_oauth_mcp import oauth
from chatgpt_web_oauth_mcp.oauth import (
    REFRESH_TOKEN_REUSE_GRACE_SECONDS,
    OAuthManager,
    OAuthRuntimeConfig,
)

BASE = "https://mcp.example.test"
SCOPE = "local-ops"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
DAY = 24 * 60 * 60


class Clock:
    def __init__(self, now: int = 2_000_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(oauth.time, "time", c)
    return c


def _manager(tmp_path: Path, *, access_ttl: int = 30 * DAY, refresh_ttl: int = 180 * DAY) -> OAuthManager:
    return OAuthManager(
        OAuthRuntimeConfig(
            auth_mode="oauth",
            auth_token="",
            public_base_url=BASE,
            state_dir=tmp_path,
            oauth_login_token="login-secret",
            oauth_scopes=(SCOPE,),
            oauth_token_ttl_seconds=access_ttl,
            oauth_refresh_token_ttl_seconds=refresh_ttl,
        ),
        mcp_path="/mcp",
    )


def _grant(manager: OAuthManager, *, scope: str | None = None) -> tuple[str, dict]:
    client_id = manager.register_client({"redirect_uris": [REDIRECT], "client_name": "ChatGPT"})["client_id"]
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    payload = {
        "login_token": "login-secret",
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": manager.resource_url(BASE),
    }
    if scope is not None:
        payload["scope"] = scope
    location = manager.authorize(payload, base_url=BASE)
    code = parse_qs(urlparse(location).query)["code"][0]
    token = manager.exchange_code(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "resource": manager.resource_url(BASE),
        },
        base_url=BASE,
    )
    return client_id, token


def _refresh(manager: OAuthManager, client_id: str, refresh_token: str, **extra: str) -> dict:
    return manager.exchange_code(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id, **extra},
        base_url=BASE,
    )


def test_metadata_and_registration_advertise_refresh_grant(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert manager.authorization_server_metadata(BASE)["grant_types_supported"] == [
        "authorization_code",
        "refresh_token",
    ]
    registered = manager.register_client({"redirect_uris": [REDIRECT]})
    assert registered["grant_types"] == ["authorization_code", "refresh_token"]


def test_code_exchange_issues_refresh_token_stored_only_as_hash(tmp_path: Path, clock: Clock) -> None:
    manager = _manager(tmp_path)
    _, token = _grant(manager)
    assert token["refresh_token"].startswith("mcp_rt_")
    assert token["expires_in"] == 30 * DAY
    raw_store = manager.store_path.read_text(encoding="utf-8")
    assert token["refresh_token"] not in raw_store
    store = json.loads(raw_store)
    (record,) = store["refresh_tokens"].values()
    assert record["expires_at"] == clock.now + 180 * DAY


def test_refresh_rotates_and_new_access_token_works(tmp_path: Path, clock: Clock) -> None:
    manager = _manager(tmp_path, access_ttl=3600)
    client_id, first = _grant(manager)
    clock.now += 7200  # first access token expired
    assert not manager.verify_access_token(first["access_token"], base_url=BASE)

    second = _refresh(manager, client_id, first["refresh_token"], resource=manager.resource_url(BASE))
    assert second["refresh_token"] != first["refresh_token"]
    assert second["scope"] == SCOPE
    assert manager.verify_access_token(second["access_token"], base_url=BASE)

    third = _refresh(manager, client_id, second["refresh_token"])
    assert manager.verify_access_token(third["access_token"], base_url=BASE)


def test_reuse_within_grace_is_a_retry_but_later_reuse_revokes_the_grant(tmp_path: Path, clock: Clock) -> None:
    manager = _manager(tmp_path)
    client_id, first = _grant(manager)
    second = _refresh(manager, client_id, first["refresh_token"])

    clock.now += REFRESH_TOKEN_REUSE_GRACE_SECONDS - 1
    retry = _refresh(manager, client_id, first["refresh_token"])  # concurrent retry
    assert manager.verify_access_token(retry["access_token"], base_url=BASE)

    clock.now += 2  # now beyond the grace window
    with pytest.raises(ValueError, match="reuse detected"):
        _refresh(manager, client_id, first["refresh_token"])
    for live in (second["refresh_token"], retry["refresh_token"]):
        with pytest.raises(ValueError, match="invalid or expired"):
            _refresh(manager, client_id, live)


def test_refresh_token_expires_but_rotation_slides_the_window(tmp_path: Path, clock: Clock) -> None:
    manager = _manager(tmp_path)
    client_id, first = _grant(manager)
    clock.now += 179 * DAY
    second = _refresh(manager, client_id, first["refresh_token"])
    clock.now += 179 * DAY  # 358 days after the original grant
    third = _refresh(manager, client_id, second["refresh_token"])
    assert manager.verify_access_token(third["access_token"], base_url=BASE)

    clock.now += 181 * DAY
    with pytest.raises(ValueError, match="invalid or expired"):
        _refresh(manager, client_id, third["refresh_token"])


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"client_id": "mcp_client_other"}, "client_id mismatch"),
        ({"resource": "https://evil.example.test/mcp"}, "resource does not match"),
        ({"scope": SCOPE + " admin"}, "exceeds the original grant"),
        ({"refresh_token": "mcp_rt_unknown"}, "invalid or expired"),
        ({"refresh_token": ""}, "refresh_token is required"),
    ],
)
def test_refresh_rejections(tmp_path: Path, clock: Clock, override: dict, message: str) -> None:
    manager = _manager(tmp_path)
    client_id, first = _grant(manager)
    payload = {"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": client_id}
    payload.update(override)
    with pytest.raises(ValueError, match=message):
        manager.exchange_code(payload, base_url=BASE)


def test_offline_access_is_tolerated_and_not_stored(tmp_path: Path, clock: Clock) -> None:
    manager = _manager(tmp_path)
    client_id, first = _grant(manager, scope=SCOPE + " offline_access")
    assert first["scope"] == SCOPE
    assert manager.verify_access_token(first["access_token"], base_url=BASE)
    second = _refresh(manager, client_id, first["refresh_token"], scope="offline_access")
    assert second["scope"] == SCOPE


def test_legacy_store_without_refresh_tokens_keeps_working(tmp_path: Path, clock: Clock) -> None:
    manager = _manager(tmp_path)
    client_id, first = _grant(manager)
    store = json.loads(manager.store_path.read_text(encoding="utf-8"))
    store.pop("refresh_tokens")
    manager.store_path.write_text(json.dumps(store), encoding="utf-8")
    assert manager.verify_access_token(first["access_token"], base_url=BASE)
    with pytest.raises(ValueError, match="invalid or expired"):
        _refresh(manager, client_id, first["refresh_token"])
    _, again = _grant(manager)
    assert "refresh_tokens" in json.loads(manager.store_path.read_text(encoding="utf-8"))
    assert again["refresh_token"]


def test_rotated_records_are_pruned_after_the_retention_window(tmp_path: Path, clock: Clock) -> None:
    manager = _manager(tmp_path)
    client_id, first = _grant(manager)
    second = _refresh(manager, client_id, first["refresh_token"])
    clock.now += oauth.ROTATED_REFRESH_TOKEN_RETENTION_SECONDS + 1
    third = _refresh(manager, client_id, second["refresh_token"])
    store = json.loads(manager.store_path.read_text(encoding="utf-8"))
    assert len(store["refresh_tokens"]) == 2  # second (rotated, recent) + third; first pruned
    with pytest.raises(ValueError, match="invalid or expired"):
        _refresh(manager, client_id, first["refresh_token"])
    assert third["refresh_token"]
