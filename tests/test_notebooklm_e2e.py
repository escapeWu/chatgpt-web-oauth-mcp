from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import UTC, datetime
from typing import Any

import pytest

from chatgpt_web_oauth_mcp.notebooklm import (
    NotebookLMAuthError,
    NotebookLMConfig,
    create_client,
    open_client,
)


# Live NotebookLM tests are opt-in because they require notebooklm-py plus a
# Google/NotebookLM session. Safe local runs:
#
#   NOTEBOOKLM_E2E=0 pytest -q tests/test_notebooklm_e2e.py -v
#   NOTEBOOKLM_E2E=1 NOTEBOOKLM_PROFILE=work pytest -q tests/test_notebooklm_e2e.py -v
#   NOTEBOOKLM_E2E=1 NOTEBOOKLM_STORAGE_PATH=/path/storage_state.json pytest -q tests/test_notebooklm_e2e.py -v
#
# Env contract:
# - NOTEBOOKLM_E2E=1 enables these live tests; any other value skips them.
# - NOTEBOOKLM_STORAGE_PATH, NOTEBOOKLM_PROFILE, and
#   NOTEBOOKLM_TIMEOUT_SECONDS reuse the runtime NotebookLM configuration.
# - NOTEBOOKLM_E2E_NOTEBOOK_ID, CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID, or
#   NOTEBOOKLM_NOTEBOOK provide the notebook used by the source/chat flow.
# - NOTEBOOKLM_E2E_CREATE_TEMP=1 allows creating a temporary notebook only
#   when notebooklm-py exposes notebooks.delete for safe cleanup.

pytestmark = pytest.mark.skipif(
    os.environ.get("NOTEBOOKLM_E2E", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="live NotebookLM E2E disabled; set NOTEBOOKLM_E2E=1 to enable",
)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _timeout_seconds() -> int:
    value = os.environ.get("NOTEBOOKLM_TIMEOUT_SECONDS", "30").strip() or "30"
    return int(value)


def _live_config() -> NotebookLMConfig:
    return NotebookLMConfig(
        enabled=True,
        storage_path=os.environ.get("NOTEBOOKLM_STORAGE_PATH", "").strip(),
        profile=os.environ.get("NOTEBOOKLM_PROFILE", "").strip(),
        default_notebook_id=(
            os.environ.get("NOTEBOOKLM_E2E_NOTEBOOK_ID")
            or os.environ.get("CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID")
            or os.environ.get("NOTEBOOKLM_NOTEBOOK")
            or ""
        ).strip(),
        timeout_seconds=_timeout_seconds(),
    )


def _has_explicit_auth_locator() -> bool:
    return bool(os.environ.get("NOTEBOOKLM_STORAGE_PATH") or os.environ.get("NOTEBOOKLM_PROFILE"))


def _dependency_available() -> bool:
    return importlib.util.find_spec("notebooklm") is not None


def _call(coro):
    return asyncio.run(coro)


def _get_field(payload: Any, name: str) -> Any:
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


def _notebook_id(payload: Any) -> str:
    notebook_id = _get_field(payload, "id")
    assert isinstance(notebook_id, str) and notebook_id.strip(), payload
    return notebook_id


def _source_id(payload: Any) -> str:
    source_id = _get_field(payload, "id")
    assert isinstance(source_id, str) and source_id.strip(), payload
    return source_id


def _answer_text(payload: Any) -> str:
    answer = _get_field(payload, "answer")
    assert isinstance(answer, str) and answer.strip(), payload
    return answer


def _skip_if_unconfigured(exc: NotebookLMAuthError) -> None:
    if _has_explicit_auth_locator():
        raise AssertionError(f"NotebookLM auth locator was provided but auth failed: {exc}") from exc
    pytest.skip(
        "NotebookLM auth is not available from notebooklm-py's default profile. "
        "Set NOTEBOOKLM_PROFILE or NOTEBOOKLM_STORAGE_PATH, then rerun with NOTEBOOKLM_E2E=1."
    )


def _require_live_dependency() -> None:
    if not _dependency_available():
        pytest.skip("notebooklm-py is not installed; install the notebooklm extra to run live E2E")


def test_notebooklm_live_auth_check_and_notebook_list() -> None:
    _require_live_dependency()

    async def scenario() -> None:
        client = create_client(_live_config())
        auth = await client.auth_check()
        notebooks = await client.list_notebooks()

        assert auth["authenticated"] is True
        assert isinstance(auth["notebook_count"], int)
        assert isinstance(notebooks, list)
        assert auth["notebook_count"] == len(notebooks)

    try:
        _call(scenario())
    except NotebookLMAuthError as exc:
        _skip_if_unconfigured(exc)


def test_notebooklm_live_source_and_chat_flow() -> None:
    _require_live_dependency()

    async def scenario() -> None:
        config = _live_config()
        notebook_id = config.default_notebook_id_value
        created_notebook_id: str | None = None
        created_source_id: str | None = None

        async with open_client(config) as client:
            raw_notebooks = getattr(client.raw_client, "notebooks", None)
            raw_sources = getattr(client.raw_client, "sources", None)
            raw_chat = getattr(client.raw_client, "chat", None)
            can_delete_notebook = callable(getattr(raw_notebooks, "delete", None))
            can_delete_source = callable(getattr(raw_sources, "delete", None))

            if not can_delete_source:
                pytest.skip(
                    "source/chat E2E requires notebooklm-py sources.delete so the test-created source can be cleaned up"
                )
            if not callable(getattr(raw_sources, "add_text", None)):
                pytest.skip("source/chat E2E requires notebooklm-py sources.add_text")
            if not callable(getattr(raw_sources, "list", None)):
                pytest.skip("source/chat E2E requires notebooklm-py sources.list")
            if not callable(getattr(raw_chat, "ask", None)):
                pytest.skip("source/chat E2E requires notebooklm-py chat.ask")

            if not notebook_id:
                if not _env_flag("NOTEBOOKLM_E2E_CREATE_TEMP"):
                    pytest.skip(
                        "source/chat E2E needs NOTEBOOKLM_E2E_NOTEBOOK_ID, "
                        "CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID, NOTEBOOKLM_NOTEBOOK, "
                        "or NOTEBOOKLM_E2E_CREATE_TEMP=1"
                    )
                if not callable(getattr(raw_notebooks, "create", None)):
                    pytest.skip("NOTEBOOKLM_E2E_CREATE_TEMP=1 requires notebooklm-py notebooks.create")
                if not can_delete_notebook:
                    pytest.skip(
                        "NOTEBOOKLM_E2E_CREATE_TEMP=1 requires notebooklm-py notebooks.delete "
                        "so the temporary notebook can be cleaned up"
                    )
                notebook = await client.create_notebook(
                    f"Codex NotebookLM E2E {datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
                )
                created_notebook_id = _notebook_id(notebook)
                notebook_id = created_notebook_id

            assert notebook_id
            token = f"codex-e2e-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            source = await client.add_text_source(
                notebook_id,
                f"Codex E2E source {token}",
                f"This is a temporary NotebookLM E2E source. Unique token: {token}.",
                wait=True,
                wait_timeout=float(config.timeout_seconds),
            )
            created_source_id = _source_id(source)

            try:
                sources = await client.list_sources(notebook_id)
                assert any(_get_field(item, "id") == created_source_id for item in sources)

                answer = await client.ask(
                    notebook_id,
                    "What unique token appears in the Codex E2E source? Answer briefly.",
                    source_ids=[created_source_id],
                )
                assert _answer_text(answer)
            finally:
                if created_source_id:
                    await client.delete_source(notebook_id, created_source_id)
                if created_notebook_id:
                    await client.raw_client.notebooks.delete(created_notebook_id)

    try:
        _call(scenario())
    except NotebookLMAuthError as exc:
        _skip_if_unconfigured(exc)
