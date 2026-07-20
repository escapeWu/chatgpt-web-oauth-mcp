"""CAS-protected, lossless mechanical batch replacement."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterator

from .content_io import (
    DecodedText,
    TextDecodingError,
    decode_text_bytes,
    encode_text_bytes,
    normalize_newlines,
)

try:  # pragma: no cover - Windows fallback is exercised on Windows CI.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {"code": code, "message": message},
    }
    payload.update(extra)
    return payload


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _lock_path(path: Path) -> Path:
    root = Path(tempfile.gettempdir()) / "chatgpt-web-oauth-mcp" / "replace-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    name = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()
    return root / f"{name}.lock"


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    lock_path = _lock_path(path)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:  # pragma: no cover
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        else:  # pragma: no cover
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def _atomic_write(path: Path, raw: bytes, *, mode: int) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".replace-tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _regex_flags(rule: dict[str, object]) -> int:
    flags = 0
    if bool(rule.get("ignore_case", False)):
        flags |= re.IGNORECASE
    if bool(rule.get("multiline", False)):
        flags |= re.MULTILINE
    if bool(rule.get("dot_all", False)):
        flags |= re.DOTALL
    return flags


def _apply_rule(
    text: str,
    *,
    rule: dict[str, object],
    decoded: DecodedText,
    remaining_budget: int,
) -> tuple[str, int] | dict[str, object]:
    pattern = rule.get("pattern")
    replacement = rule.get("replacement")
    if not isinstance(pattern, str) or not pattern:
        return _error("invalid_rule", "Each rule requires a non-empty string pattern.")
    if not isinstance(replacement, str):
        return _error("invalid_rule", "Each rule requires a string replacement.")
    raw_count = rule.get("count", 0)
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        return _error("invalid_rule", "rule.count must be a non-negative integer.")
    count = raw_count
    if count < 0:
        return _error("invalid_rule", "rule.count must be a non-negative integer.")

    replacement = normalize_newlines(replacement, decoded.newline)
    literal = bool(rule.get("literal", True))
    flags = _regex_flags(rule)
    if literal and not flags:
        available = text.count(pattern)
        replacements = min(available, count) if count else available
        if replacements > remaining_budget:
            return _error(
                "max_replacements_exceeded",
                "Replacement count exceeds max_replacements; no files were changed.",
                attempted=replacements,
            )
        return text.replace(pattern, replacement, replacements), replacements

    try:
        expression = re.compile(re.escape(pattern) if literal else pattern, flags)
    except re.error as exc:
        return _error("invalid_pattern", f"Invalid replacement regular expression: {exc}")

    maximum = count if count else remaining_budget + 1
    matches = 0
    for _match in expression.finditer(text):
        matches += 1
        if matches >= maximum:
            break
    replacements = min(matches, count) if count else matches
    if replacements > remaining_budget or (not count and matches > remaining_budget):
        return _error(
            "max_replacements_exceeded",
            "Replacement count exceeds max_replacements; no files were changed.",
            attempted=matches,
        )
    replacement_value: Any
    if literal:
        replacement_value = lambda _match: replacement
    else:
        replacement_value = replacement
    replaced, actual = expression.subn(replacement_value, text, count=count)
    return replaced, actual


def replace_files(
    operations: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    max_replacements: int = 10000,
) -> dict[str, object]:
    if not operations:
        return _error("invalid_arguments", "Provide at least one replacement operation.")
    try:
        effective_max = int(max_replacements)
    except (TypeError, ValueError):
        return _error("invalid_arguments", "max_replacements must be a positive integer.")
    if effective_max <= 0:
        return _error("invalid_arguments", "max_replacements must be a positive integer.")

    grouped: dict[Path, dict[str, Any]] = {}
    for operation in operations:
        path = operation.get("path")
        rules = operation.get("rules")
        if not isinstance(path, Path):
            return _error("invalid_arguments", "Each operation requires a resolved path.")
        if not isinstance(rules, list) or not rules or any(not isinstance(rule, dict) for rule in rules):
            return _error("invalid_arguments", "Each operation requires a non-empty rules list.")
        entry = grouped.setdefault(
            path,
            {
                "rules": [],
                "expected_revision": operation.get("expected_revision"),
                "encoding": operation.get("encoding"),
            },
        )
        if entry["encoding"] != operation.get("encoding"):
            return _error("invalid_arguments", f"Conflicting encodings for {path}.")
        if entry["expected_revision"] != operation.get("expected_revision"):
            return _error("invalid_arguments", f"Conflicting expected revisions for {path}.")
        entry["rules"].extend(rules)

    ordered_paths = sorted(grouped, key=lambda item: str(item.resolve(strict=False)))
    with ExitStack() as locks:
        for path in ordered_paths:
            locks.enter_context(_exclusive_lock(path))

        plans: list[dict[str, Any]] = []
        total_replacements = 0
        for path in ordered_paths:
            if not path.exists():
                return _error("file_not_found", f"File not found: {path}", resolved_path=str(path))
            if not path.is_file():
                return _error("not_a_file", f"Path is not a file: {path}", resolved_path=str(path))
            raw = path.read_bytes()
            before_revision = _revision(raw)
            expected_revision = grouped[path]["expected_revision"]
            if expected_revision is not None and expected_revision != before_revision:
                return _error(
                    "revision_conflict",
                    f"Revision conflict for {path}; no files were changed.",
                    resolved_path=str(path),
                    expected_revision=expected_revision,
                    actual_revision=before_revision,
                )
            try:
                decoded = decode_text_bytes(raw, encoding=grouped[path]["encoding"])
            except TextDecodingError as exc:
                return _error(
                    "encoding_error",
                    str(exc),
                    resolved_path=str(path),
                    encoding_candidates=exc.candidates,
                )

            replaced = decoded.text
            file_replacements = 0
            rule_results: list[dict[str, object]] = []
            for rule_index, rule in enumerate(grouped[path]["rules"]):
                outcome = _apply_rule(
                    replaced,
                    rule=rule,
                    decoded=decoded,
                    remaining_budget=effective_max - total_replacements - file_replacements,
                )
                if isinstance(outcome, dict):
                    outcome["path"] = str(path)
                    outcome["rule_index"] = rule_index
                    return outcome
                replaced, replacements = outcome
                file_replacements += replacements
                rule_results.append({"rule_index": rule_index, "replacements": replacements})

            after_raw = encode_text_bytes(decoded, replaced)
            total_replacements += file_replacements
            plans.append(
                {
                    "path": path,
                    "before_raw": raw,
                    "after_raw": after_raw,
                    "before_revision": before_revision,
                    "after_revision": _revision(after_raw),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                    "encoding": decoded.encoding,
                    "bom": decoded.bom,
                    "newline": decoded.newline,
                    "replacements": file_replacements,
                    "rules": rule_results,
                }
            )

        # Recheck every target immediately before the first commit. This makes
        # the batch all-or-nothing for cooperative writers using the same locks
        # and detects non-cooperative changes during planning.
        for plan in plans:
            path = plan["path"]
            if _revision(path.read_bytes()) != plan["before_revision"]:
                return _error(
                    "revision_conflict",
                    f"File changed while replacements were planned: {path}.",
                    resolved_path=str(path),
                    expected_revision=plan["before_revision"],
                    actual_revision=_revision(path.read_bytes()),
                )

        attempted: list[dict[str, Any]] = []
        if not dry_run:
            try:
                for plan in plans:
                    if plan["before_raw"] == plan["after_raw"]:
                        continue
                    # Include the current plan before the atomic call because an
                    # exception can occur after os.replace (for example during
                    # directory fsync). Such a target must still be rolled back.
                    attempted.append(plan)
                    _atomic_write(plan["path"], plan["after_raw"], mode=plan["mode"])
                    if _revision(plan["path"].read_bytes()) != plan["after_revision"]:
                        raise OSError(f"post-write revision verification failed for {plan['path']}")
            except Exception as exc:
                rollback_errors: list[str] = []
                for plan in reversed(attempted):
                    try:
                        _atomic_write(plan["path"], plan["before_raw"], mode=plan["mode"])
                        if _revision(plan["path"].read_bytes()) != plan["before_revision"]:
                            raise OSError("rollback revision verification failed")
                    except Exception as rollback_exc:
                        rollback_errors.append(f"{plan['path']}: {rollback_exc}")
                return _error(
                    "write_failed",
                    (
                        f"Atomic replacement failed: {exc}. "
                        + (
                            "Rollback also failed for: " + "; ".join(rollback_errors)
                            if rollback_errors
                            else "All attempted writes were rolled back."
                        )
                    ),
                    rolled_back=not rollback_errors,
                    rollback_errors=rollback_errors,
                )

    return {
        "success": True,
        "dry_run": dry_run,
        "written": not dry_run,
        "changed_files": sum(plan["before_raw"] != plan["after_raw"] for plan in plans),
        "total_replacements": total_replacements,
        "max_replacements": effective_max,
        "files": [
            {
                "path": str(plan["path"]),
                "changed": plan["before_raw"] != plan["after_raw"],
                "replacements": plan["replacements"],
                "rules": plan["rules"],
                "before_revision": plan["before_revision"],
                "after_revision": plan["after_revision"],
                "encoding": plan["encoding"],
                "bom": plan["bom"],
                "newline": plan["newline"],
                "permissions": oct(plan["mode"]),
            }
            for plan in plans
        ],
    }
