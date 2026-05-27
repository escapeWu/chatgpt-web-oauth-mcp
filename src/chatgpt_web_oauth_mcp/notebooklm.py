from __future__ import annotations

import dataclasses
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, AsyncIterator, Awaitable, Callable


class NotebookLMConfigError(RuntimeError):
    pass


class NotebookLMDependencyError(NotebookLMConfigError):
    pass


class NotebookLMAuthError(NotebookLMConfigError):
    pass


class NotebookLMRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class NotebookLMConfig:
    enabled: bool = False
    storage_path: str = ""
    profile: str = ""
    default_notebook_id: str = ""
    timeout_seconds: int = 30

    @property
    def storage_path_value(self) -> str | None:
        if not self.storage_path.strip():
            return None
        return str(Path(self.storage_path).expanduser())

    @property
    def profile_value(self) -> str | None:
        return self.profile.strip() or None

    @property
    def default_notebook_id_value(self) -> str | None:
        return self.default_notebook_id.strip() or None

    @property
    def configured(self) -> bool:
        # notebooklm-py can resolve its active/default profile, so explicit
        # auth locator env vars are optional once NotebookLM support is enabled.
        return self.enabled

    @property
    def storage_label(self) -> str:
        return self.storage_path_value or "notebooklm default storage"


def _serialize_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _serialize_jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_serialize_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize_jsonable(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return {
            str(key): _serialize_jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _serialize_model(value: Any) -> Any:
    return _serialize_jsonable(value)


def _pick_fields(value: Any, fields: tuple[str, ...]) -> dict[str, object]:
    data = _serialize_jsonable(value)
    if not isinstance(data, dict):
        return {"value": data}
    return {field: data[field] for field in fields if field in data}


def compact_notebook(value: Any) -> dict[str, object]:
    return _pick_fields(value, ("id", "title", "created_at", "sources_count", "is_owner"))


def compact_source(value: Any) -> dict[str, object]:
    return _pick_fields(value, ("id", "title", "url", "type", "_type_code", "created_at", "status"))


def compact_answer(value: Any) -> dict[str, object]:
    return _pick_fields(value, ("answer", "conversation_id", "turn_number", "is_follow_up", "references"))


def _load_notebooklm(importer: Callable[[str], ModuleType] = import_module) -> ModuleType:
    try:
        return importer("notebooklm")
    except ModuleNotFoundError as exc:
        if exc.name == "notebooklm":
            raise NotebookLMDependencyError(
                "NotebookLM support requires the optional dependency notebooklm-py. "
                "Install it with `pip install 'chatgpt-web-oauth-mcp[notebooklm]'` "
                "or `pip install notebooklm-py`."
            ) from exc
        raise


def _client_class(notebooklm_module: ModuleType) -> Any:
    client_cls = getattr(notebooklm_module, "NotebookLMClient", None)
    if client_cls is None:
        raise NotebookLMDependencyError(
            "The installed notebooklm-py package does not expose NotebookLMClient. "
            "Upgrade notebooklm-py to a supported version."
        )
    return client_cls


def _auth_setup_message(config: NotebookLMConfig, exc: Exception) -> str:
    locator = []
    if config.storage_path_value:
        locator.append(f"storage_path={config.storage_path_value}")
    if config.profile_value:
        locator.append(f"profile={config.profile_value}")
    locator_text = f" Current locator: {', '.join(locator)}." if locator else ""
    return (
        "NotebookLM authentication is not configured or could not be loaded. "
        "Run `notebooklm login`, set NOTEBOOKLM_STORAGE_PATH to a valid "
        "storage_state.json, or set NOTEBOOKLM_PROFILE to an authenticated "
        f"notebooklm-py profile.{locator_text} Original error: {exc}"
    )


def _is_auth_setup_error(exc: Exception) -> bool:
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return True
    name = type(exc).__name__.lower()
    if any(part in name for part in ("auth", "configuration", "validation")):
        return True
    message = str(exc).lower()
    return any(part in message for part in ("auth", "cookie", "csrf", "storage_state", "storage state"))


@asynccontextmanager
async def _open_raw_client(
    config: NotebookLMConfig,
    *,
    importer: Callable[[str], ModuleType] = import_module,
) -> AsyncIterator[Any]:
    if not config.enabled:
        raise NotebookLMConfigError(
            "NotebookLM support is disabled. Set CHATGPT_MCP_ENABLE_NOTEBOOKLM=1 "
            "to enable NotebookLM tools."
        )

    notebooklm_module = _load_notebooklm(importer)
    client_cls = _client_class(notebooklm_module)

    try:
        context = client_cls.from_storage(
            path=config.storage_path_value,
            profile=config.profile_value,
            timeout=config.timeout_seconds,
        )
    except Exception as exc:
        if _is_auth_setup_error(exc):
            raise NotebookLMAuthError(_auth_setup_message(config, exc)) from exc
        raise NotebookLMRequestError(str(exc)) from exc

    try:
        async with context as raw_client:
            yield raw_client
    except (NotebookLMConfigError, NotebookLMRequestError):
        raise
    except Exception as exc:
        if _is_auth_setup_error(exc):
            raise NotebookLMAuthError(_auth_setup_message(config, exc)) from exc
        raise NotebookLMRequestError(str(exc)) from exc


class NotebookLMClientWrapper:
    """Small async boundary around the optional ``notebooklm-py`` package."""

    def __init__(self, config: NotebookLMConfig, raw_client: Any | None = None) -> None:
        self.config = config
        self.raw_client = raw_client

    def require_notebook_id(self, notebook_id: str | None = None) -> str:
        resolved = (notebook_id or "").strip() or self.config.default_notebook_id_value
        if not resolved:
            raise NotebookLMConfigError(
                "NotebookLM notebook id is required. Pass notebook_id or set "
                "CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID / NOTEBOOKLM_NOTEBOOK."
            )
        return resolved

    async def _with_client(self, callback: Callable[[Any], Awaitable[Any]]) -> Any:
        if self.raw_client is not None:
            return await callback(self.raw_client)
        async with _open_raw_client(self.config) as client:
            return await callback(client)

    async def auth_check(self) -> dict[str, object]:
        notebooks = await self.list_notebooks()
        count = len(notebooks) if isinstance(notebooks, list) else None
        return {"authenticated": True, "notebook_count": count}

    async def list_notebooks(self) -> list[Any]:
        return _serialize_model(await self._with_client(lambda client: client.notebooks.list()))

    async def create_notebook(self, title: str) -> Any:
        return _serialize_model(await self._with_client(lambda client: client.notebooks.create(title)))

    async def add_text_source(
        self,
        notebook_id: str,
        title: str,
        text: str,
        *,
        wait: bool = False,
        wait_timeout: float | None = None,
    ) -> Any:
        resolved_notebook_id = self.require_notebook_id(notebook_id)

        async def _add(client: Any) -> Any:
            kwargs: dict[str, object] = {"wait": wait}
            if wait_timeout is not None:
                kwargs["wait_timeout"] = wait_timeout
            return await client.sources.add_text(resolved_notebook_id, title, text, **kwargs)

        return _serialize_model(await self._with_client(_add))

    async def delete_source(self, notebook_id: str, source_id: str) -> bool:
        resolved_notebook_id = self.require_notebook_id(notebook_id)
        return bool(await self._with_client(lambda client: client.sources.delete(resolved_notebook_id, source_id)))

    async def list_sources(self, notebook_id: str) -> list[Any]:
        resolved_notebook_id = self.require_notebook_id(notebook_id)
        return _serialize_model(await self._with_client(lambda client: client.sources.list(resolved_notebook_id)))

    async def ask(
        self,
        notebook_id: str,
        question: str,
        *,
        source_ids: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> Any:
        resolved_notebook_id = self.require_notebook_id(notebook_id)
        return _serialize_model(
            await self._with_client(
                lambda client: client.chat.ask(
                    resolved_notebook_id,
                    question,
                    source_ids=source_ids,
                    conversation_id=conversation_id,
                )
            )
        )


@asynccontextmanager
async def open_client(
    config: NotebookLMConfig,
    *,
    importer: Callable[[str], ModuleType] = import_module,
) -> AsyncIterator[NotebookLMClientWrapper]:
    async with _open_raw_client(config, importer=importer) as raw_client:
        yield NotebookLMClientWrapper(config=config, raw_client=raw_client)


async def with_client(config: NotebookLMConfig, operation: Callable[[NotebookLMClientWrapper], Any]) -> Any:
    async with open_client(config) as client:
        result = operation(client)
        if hasattr(result, "__await__"):
            return await result
        return result


def create_client(config: NotebookLMConfig) -> NotebookLMClientWrapper:
    return NotebookLMClientWrapper(config)


def proxy_error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, NotebookLMDependencyError):
        code = "notebooklm_dependency_missing"
    elif isinstance(exc, NotebookLMAuthError):
        code = "notebooklm_auth_not_configured"
    elif isinstance(exc, NotebookLMConfigError):
        code = "notebooklm_not_configured"
    else:
        code = "notebooklm_request_failed"
    return {"success": False, "error": {"code": code, "message": str(exc)}}


def client_error(exc: Exception) -> dict[str, object]:
    return proxy_error(exc)
