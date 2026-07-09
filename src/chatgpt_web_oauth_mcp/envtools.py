from __future__ import annotations

import hashlib
import platform as platform_module
import subprocess
import sys
from pathlib import Path
from typing import Any


COMMON_ENV_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle",
)
COMMAND_TIMEOUT_SECONDS = 5
PACKAGE_TIMEOUT_SECONDS = 10
MAX_PACKAGE_LINES = 2000

_MISSING = object()


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    payload.update(extra)
    return payload


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> dict[str, object]:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": {
                "code": "command_not_found",
                "message": str(exc),
            },
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": {
                "code": "command_timeout",
                "message": f"Command timed out after {timeout}s.",
            },
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": {
                "code": "command_failed",
                "message": str(exc),
            },
        }
    payload: dict[str, object] = {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if result.returncode != 0:
        payload["error"] = {
            "code": "command_nonzero",
            "message": result.stderr.strip() or result.stdout.strip() or f"Command exited with {result.returncode}.",
        }
    return payload


def _first_output_line(result: dict[str, object]) -> str | None:
    for key in ("stdout", "stderr"):
        value = result.get(key)
        if isinstance(value, str):
            for line in value.splitlines():
                normalized = line.strip()
                if normalized:
                    return normalized
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> dict[str, object]:
    payload: dict[str, object] = {
        "exists": path.exists(),
    }
    if path.exists():
        payload["is_file"] = path.is_file()
    if path.is_file():
        try:
            payload["sha256"] = _sha256(path)
        except OSError as exc:
            payload["error"] = {
                "code": "hash_failed",
                "message": str(exc),
            }
    return payload


def _python_snapshot() -> dict[str, object]:
    prefix = sys.prefix
    base_prefix = getattr(sys, "base_prefix", prefix)
    return {
        "executable": sys.executable,
        "version": platform_module.python_version(),
        "is_venv": bool(prefix != base_prefix or getattr(sys, "real_prefix", None)),
        "prefix": prefix,
        "base_prefix": base_prefix,
    }


def _git_snapshot(cwd: Path) -> dict[str, object]:
    version = _run(["git", "--version"], cwd=cwd)
    if not version.get("ok"):
        return {
            "available": False,
            "version": None,
            "inside_repo": False,
            "branch": None,
            "head": None,
            "error": version.get("error"),
        }

    inside = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd)
    inside_repo = bool(inside.get("ok") and str(inside.get("stdout", "")).strip() == "true")
    branch: str | None = None
    head: str | None = None
    if inside_repo:
        branch_result = _run(["git", "branch", "--show-current"], cwd=cwd)
        if branch_result.get("ok"):
            branch = str(branch_result.get("stdout", "")).strip() or None
        head_result = _run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)
        if head_result.get("ok"):
            head = str(head_result.get("stdout", "")).strip() or None

    return {
        "available": True,
        "version": str(version.get("stdout", "")).strip() or None,
        "inside_repo": inside_repo,
        "branch": branch,
        "head": head,
    }


def _node_snapshot(cwd: Path) -> dict[str, object]:
    node = _run(["node", "--version"], cwd=cwd)
    npm = _run(["npm", "--version"], cwd=cwd)
    payload: dict[str, object] = {
        "available": bool(node.get("ok")),
        "version": (str(node.get("stdout", "")).strip() or None) if node.get("ok") else None,
        "npm_version": (str(npm.get("stdout", "")).strip() or None) if npm.get("ok") else None,
    }
    if not node.get("ok"):
        payload["error"] = node.get("error")
    elif not npm.get("ok"):
        payload["npm_error"] = npm.get("error")
    return payload


def _java_snapshot(cwd: Path) -> dict[str, object]:
    java = _run(["java", "-version"], cwd=cwd)
    payload: dict[str, object] = {
        "available": bool(java.get("ok")),
        "version": _first_output_line(java) if java.get("ok") else None,
    }
    if not java.get("ok"):
        payload["error"] = java.get("error")
    return payload


def _packages_snapshot(cwd: Path, *, include_packages: bool) -> dict[str, object]:
    if not include_packages:
        return {"included": False}

    result = _run(
        [sys.executable, "-m", "pip", "freeze"],
        cwd=cwd,
        timeout=PACKAGE_TIMEOUT_SECONDS,
    )
    if not result.get("ok"):
        return {
            "included": True,
            "success": False,
            "error": result.get("error")
            or {
                "code": "pip_freeze_failed",
                "message": str(result.get("stderr") or result.get("stdout") or "pip freeze failed."),
            },
        }

    packages = [
        line.strip()
        for line in str(result.get("stdout", "")).splitlines()
        if line.strip()
    ]
    return {
        "included": True,
        "success": True,
        "command": [sys.executable, "-m", "pip", "freeze"],
        "count": len(packages),
        "items": packages[:MAX_PACKAGE_LINES],
        "truncated": len(packages) > MAX_PACKAGE_LINES,
    }


def env_snapshot(*, cwd: Path, include_packages: bool = False) -> dict[str, object]:
    if not cwd.exists():
        return _error("cwd_not_found", f"Working directory not found: {cwd}", cwd=str(cwd))
    if not cwd.is_dir():
        return _error("cwd_not_directory", f"Path is not a directory: {cwd}", cwd=str(cwd))

    files = {
        filename: _file_fingerprint(cwd / filename)
        for filename in COMMON_ENV_FILES
    }
    return {
        "success": True,
        "cwd": str(cwd),
        "platform": {
            "system": platform_module.system(),
            "release": platform_module.release(),
            "machine": platform_module.machine(),
        },
        "python": _python_snapshot(),
        "git": _git_snapshot(cwd),
        "node": _node_snapshot(cwd),
        "java": _java_snapshot(cwd),
        "files": files,
        "packages": _packages_snapshot(cwd, include_packages=include_packages),
    }


def _public_value(value: object) -> object:
    if value is _MISSING:
        return None
    return value


def _diff_values(
    left: object,
    right: object,
    *,
    path: str,
    changes: dict[str, list[object]],
) -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        keys = sorted(set(left) | set(right), key=str)
        for key in keys:
            child_path = f"{path}.{key}" if path else str(key)
            _diff_values(
                left.get(key, _MISSING),
                right.get(key, _MISSING),
                path=child_path,
                changes=changes,
            )
        return

    if left != right:
        changes[path or "$"] = [_public_value(left), _public_value(right)]


def env_diff(*, left: dict[str, Any], right: dict[str, Any]) -> dict[str, object]:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return _error(
            "invalid_arguments",
            "left and right must both be JSON-like objects.",
        )
    changes: dict[str, list[object]] = {}
    _diff_values(left, right, path="", changes=changes)
    return {
        "success": True,
        "changed": bool(changes),
        "changes": changes,
    }
