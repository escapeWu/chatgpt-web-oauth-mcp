from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from typing import Any

try:  # pragma: no cover - Windows fallback is exercised only on Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .errors import BindingStoreError
from .models import RuntimeBinding


BINDING_SCHEMA_VERSION = 1
BINDING_DIRECTORY_NAME = "codex-runtime"
BINDING_FILENAME = "bindings.json"
BINDING_LOCK_FILENAME = "bindings.lock"


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except OSError as exc:
        raise BindingStoreError(
            "Unable to create the private Codex runtime state directory.",
            details={"path": str(path), "errno": getattr(exc, "errno", None)},
        ) from None


@contextmanager
def _file_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    _ensure_private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise BindingStoreError(
            "Unable to open the Codex runtime state lock.",
            details={"path": str(path), "errno": getattr(exc, "errno", None)},
        ) from None
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    except OSError as exc:
        raise BindingStoreError(
            "Unable to lock the Codex runtime state.",
            details={"path": str(path), "errno": getattr(exc, "errno", None)},
        ) from None
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


class BindingStore:
    """Atomic, private, process-safe persistence for runtime bindings."""

    def __init__(self, state_dir: Path) -> None:
        self.root = Path(state_dir).expanduser().resolve() / BINDING_DIRECTORY_NAME
        self.path = self.root / BINDING_FILENAME
        self.lock_path = self.root / BINDING_LOCK_FILENAME

    def load(self) -> dict[str, RuntimeBinding]:
        if not self.root.exists():
            return {}
        with _file_lock(self.lock_path, exclusive=False):
            if not self.path.exists():
                return {}
            try:
                raw = self.path.read_text(encoding="utf-8")
                document = json.loads(raw)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise BindingStoreError(
                    "Codex runtime bindings are not valid JSON.",
                    details={"path": str(self.path), "error": type(exc).__name__},
                ) from None
            if not isinstance(document, dict) or document.get("schema_version") != BINDING_SCHEMA_VERSION:
                raise BindingStoreError(
                    "Unsupported Codex runtime binding schema.",
                    details={"path": str(self.path), "schema_version": document.get("schema_version") if isinstance(document, dict) else None},
                )
            records = document.get("runtimes", {})
            if not isinstance(records, dict):
                raise BindingStoreError(
                    "Codex runtime bindings must contain a runtimes object.",
                    details={"path": str(self.path)},
                )
            bindings: dict[str, RuntimeBinding] = {}
            try:
                for runtime_id, record in records.items():
                    binding = RuntimeBinding.from_dict(record)
                    if runtime_id != binding.runtime_id:
                        raise ValueError("runtime binding key does not match runtime_id.")
                    bindings[runtime_id] = binding
            except (TypeError, ValueError) as exc:
                raise BindingStoreError(
                    "Codex runtime bindings contain an invalid record.",
                    details={"path": str(self.path), "error": str(exc)},
                ) from None
            return bindings

    def save(self, bindings: Mapping[str, RuntimeBinding]) -> None:
        document: dict[str, Any] = {
            "schema_version": BINDING_SCHEMA_VERSION,
            "runtimes": {runtime_id: binding.to_dict() for runtime_id, binding in bindings.items()},
        }
        encoded = (
            json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with _file_lock(self.lock_path, exclusive=True):
            _ensure_private_directory(self.root)
            temporary_path: Path | None = None
            try:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{BINDING_FILENAME}.",
                    suffix=".tmp",
                    dir=str(self.root),
                )
                temporary_path = Path(temporary_name)
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
                temporary_path = None
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
                try:
                    directory_descriptor = os.open(self.root, os.O_RDONLY)
                except OSError:
                    directory_descriptor = None
                if directory_descriptor is not None:
                    try:
                        os.fsync(directory_descriptor)
                    except OSError:
                        pass
                    finally:
                        os.close(directory_descriptor)
            except OSError as exc:
                raise BindingStoreError(
                    "Unable to atomically persist Codex runtime bindings.",
                    details={"path": str(self.path), "errno": getattr(exc, "errno", None)},
                ) from None
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
