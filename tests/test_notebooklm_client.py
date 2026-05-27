from __future__ import annotations

import asyncio
import importlib
import types
from pathlib import Path
from typing import Any

import pytest

from chatgpt_web_oauth_mcp.notebooklm import (
    NotebookLMAuthError,
    NotebookLMConfig,
    NotebookLMConfigError,
    client_error,
    open_client,
)


def _call(coro):
    return asyncio.run(coro)


async def _open_once(config: NotebookLMConfig, importer):
    async with open_client(config, importer=importer) as client:
        return client


def test_disabled_notebooklm_does_not_import_dependency() -> None:
    imported: list[str] = []

    def importer(name: str):
        imported.append(name)
        raise AssertionError("dependency import should be lazy")

    with pytest.raises(NotebookLMConfigError):
        _call(_open_once(NotebookLMConfig(enabled=False), importer))

    assert imported == []


def test_missing_notebooklm_dependency_returns_setup_error() -> None:
    def importer(name: str):
        raise ModuleNotFoundError(name=name)

    with pytest.raises(NotebookLMConfigError) as excinfo:
        _call(_open_once(NotebookLMConfig(enabled=True), importer))

    payload = client_error(excinfo.value)
    assert payload["success"] is False
    assert payload["error"]["code"] == "notebooklm_dependency_missing"


def test_open_client_uses_storage_profile_timeout_and_default_notebook(tmp_path: Path) -> None:
    calls: dict[str, Any] = {}

    class FakeNotebooks:
        async def list(self):
            return [{"id": "nb-1"}]

    class FakeRawClient:
        notebooks = FakeNotebooks()

    class FakeContext:
        async def __aenter__(self):
            return FakeRawClient()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        @classmethod
        def from_storage(cls, **kwargs):
            calls.update(kwargs)
            return FakeContext()

    def importer(name: str):
        assert name == "notebooklm"
        return types.SimpleNamespace(NotebookLMClient=FakeClient)

    config = NotebookLMConfig(
        enabled=True,
        storage_path=str(tmp_path / "storage_state.json"),
        profile="work",
        default_notebook_id="nb-default",
        timeout_seconds=45,
    )

    async def scenario():
        async with open_client(config, importer=importer) as client:
            return client.require_notebook_id(), await client.list_notebooks()

    notebook_id, notebooks = _call(scenario())

    assert calls == {
        "path": str(tmp_path / "storage_state.json"),
        "profile": "work",
        "timeout": 45,
    }
    assert notebook_id == "nb-default"
    assert notebooks == [{"id": "nb-1"}]


def test_missing_notebook_id_keeps_config_error_code() -> None:
    class FakeContext:
        async def __aenter__(self):
            return types.SimpleNamespace(notebooks=types.SimpleNamespace())

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        @classmethod
        def from_storage(cls, **kwargs):
            return FakeContext()

    def importer(name: str):
        return types.SimpleNamespace(NotebookLMClient=FakeClient)

    async def scenario():
        async with open_client(NotebookLMConfig(enabled=True), importer=importer) as client:
            client.require_notebook_id()

    with pytest.raises(NotebookLMConfigError) as excinfo:
        _call(scenario())

    assert client_error(excinfo.value)["error"]["code"] == "notebooklm_not_configured"


def test_auth_load_failure_is_structured() -> None:
    class FakeContext:
        async def __aenter__(self):
            raise FileNotFoundError("storage_state.json")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClient:
        @classmethod
        def from_storage(cls, **kwargs):
            return FakeContext()

    def importer(name: str):
        return types.SimpleNamespace(NotebookLMClient=FakeClient)

    with pytest.raises(NotebookLMAuthError) as excinfo:
        _call(_open_once(NotebookLMConfig(enabled=True), importer))

    payload = client_error(excinfo.value)
    assert payload["error"]["code"] == "notebooklm_auth_not_configured"
    assert "notebooklm login" in payload["error"]["message"]


def test_notebooklm_env_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import chatgpt_web_oauth_mcp.config as config_module

    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_ENABLE_NOTEBOOKLM", "1")
        patch.setenv("NOTEBOOKLM_STORAGE_PATH", str(tmp_path / "state.json"))
        patch.setenv("NOTEBOOKLM_PROFILE", "personal")
        patch.setenv("CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID", "nb-123")
        patch.setenv("NOTEBOOKLM_TIMEOUT_SECONDS", "55")

        loaded = importlib.reload(config_module)

        assert loaded.ENABLE_NOTEBOOKLM is True
        assert loaded.NOTEBOOKLM_STORAGE_PATH == str(tmp_path / "state.json")
        assert loaded.NOTEBOOKLM_PROFILE == "personal"
        assert loaded.NOTEBOOKLM_DEFAULT_NOTEBOOK_ID == "nb-123"
        assert loaded.NOTEBOOKLM_TIMEOUT_SECONDS == 55

    importlib.reload(config_module)
