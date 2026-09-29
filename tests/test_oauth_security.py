from __future__ import annotations

import pytest

from chatgpt_web_oauth_mcp.oauth import (
    _is_allowed_redirect_uri,
    _validated_public_base_url,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://mcp.example.test", "https://mcp.example.test"),
        ("https://mcp.example.test:8443/", "https://mcp.example.test:8443"),
        ("http://localhost:8770", "http://localhost:8770"),
        ("http://127.0.0.1:8770/", "http://127.0.0.1:8770"),
        ("http://[::1]:8770", "http://[::1]:8770"),
    ],
)
def test_validated_public_base_url_accepts_canonical_origins(
    value: str,
    expected: str,
) -> None:
    assert _validated_public_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://mcp.example.test",
        "https://mcp.example.test/path",
        "https://mcp.example.test/?query=yes",
        "https://mcp.example.test/#fragment",
        "https://mcp.example.test?",
        "https://user@mcp.example.test",
        "https://user:password@mcp.example.test",
        "https://mcp.example.test:bad",
        r"https://mcp.example.test\@attacker.example",
        " https://mcp.example.test",
        "https://[::1",
    ],
)
def test_validated_public_base_url_rejects_missing_or_unsafe_origins(value: str) -> None:
    with pytest.raises(ValueError):
        _validated_public_base_url(value)


@pytest.mark.parametrize(
    "uri",
    [
        "https://client.example.test/callback",
        "https://client.example.test:8443/callback?tenant=one",
        "http://localhost/callback",
        "http://localhost:43123/callback",
        "http://127.0.0.1:43123/callback",
        "http://[::1]:43123/callback",
    ],
)
def test_allowed_redirect_uri_accepts_https_and_loopback_http(uri: str) -> None:
    assert _is_allowed_redirect_uri(uri) is True


@pytest.mark.parametrize(
    "uri",
    [
        "http://client.example.test/callback",
        "https://client.example.test/callback#fragment",
        "https://client.example.test/callback#",
        "https://user@client.example.test/callback",
        "https://user:password@client.example.test/callback",
        r"https://client.example.test\@attacker.example/callback",
        " https://client.example.test/callback",
        "https://client.example.test/callback\n",
        "https://client.example.test:bad/callback",
        "https://[::1/callback",
        "/relative/callback",
        "",
    ],
)
def test_allowed_redirect_uri_rejects_ambiguous_or_unsafe_values(uri: str) -> None:
    assert _is_allowed_redirect_uri(uri) is False
