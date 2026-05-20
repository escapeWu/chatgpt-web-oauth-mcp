from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

from .files import (
    DEFAULT_EXCLUDE_DIR_NAMES,
    _find_git_root,
    _git_tracked_allowed_paths,
    _iter_filtered,
)


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


def _validate_existing_path(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return _error(
            "path_not_found",
            f"Path not found: {path}",
            resolved_path=str(path),
        )
    return None


def _validate_directory(path: Path) -> dict[str, object] | None:
    validation_error = _validate_existing_path(path)
    if validation_error:
        return validation_error
    if not path.is_dir():
        return _error(
            "not_a_directory",
            f"Path is not a directory: {path}",
            resolved_path=str(path),
        )
    return None


def _paginate(items: list[object], *, offset: int, limit: int) -> tuple[list[object], bool, int | None]:
    start = max(offset, 0)
    if limit == 0:
        selected = items[start:]
    else:
        selected = items[start : start + max(limit, 0)]
    truncated = start + len(selected) < len(items)
    next_offset = start + len(selected) if truncated else None
    return selected, truncated, next_offset


def _resolve_allowed_paths(base_path: Path, *, respect_gitignore: bool) -> tuple[set[Path] | None, bool]:
    if not respect_gitignore:
        return None, False

    repo_root = _find_git_root(base_path)
    if repo_root is None:
        return None, False

    allowed = _git_tracked_allowed_paths(repo_root)
    return allowed, allowed is not None


def _matches_exclude_patterns(
    path: Path,
    *,
    base_path: Path,
    exclude_patterns: tuple[str, ...],
) -> bool:
    if not exclude_patterns:
        return False
    try:
        relative = str(path.relative_to(base_path if base_path.is_dir() else base_path.parent))
    except ValueError:
        relative = path.name
    return any(fnmatch(path.name, pattern) or fnmatch(relative, pattern) for pattern in exclude_patterns)


def _glob_matches(path: Path, *, base_path: Path, pattern: str) -> bool:
    try:
        relative = str(path.relative_to(base_path if base_path.is_dir() else base_path.parent))
    except ValueError:
        relative = path.name
    return fnmatch(relative, pattern) or fnmatch(path.name, pattern)


def _iter_matching_entries(
    base_path: Path,
    *,
    pattern: str,
    include_hidden: bool,
    respect_gitignore: bool,
    exclude_patterns: tuple[str, ...],
) -> tuple[list[Path], bool]:
    allowed, gitignore_applied = _resolve_allowed_paths(
        base_path,
        respect_gitignore=respect_gitignore,
    )

    if base_path.is_file():
        if not include_hidden and base_path.name.startswith("."):
            return [], gitignore_applied
        if _matches_exclude_patterns(base_path, base_path=base_path, exclude_patterns=exclude_patterns):
            return [], gitignore_applied
        if allowed is not None and base_path.resolve() not in allowed:
            return [], gitignore_applied
        return ([base_path] if _glob_matches(base_path, base_path=base_path, pattern=pattern) else []), gitignore_applied

    entries = sorted(
        _iter_filtered(
            base_path,
            recursive=True,
            include_hidden=include_hidden,
            exclude_dir_names=DEFAULT_EXCLUDE_DIR_NAMES,
            exclude_patterns=exclude_patterns,
            allowed=allowed,
        ),
        key=lambda item: str(item),
    )
    matches = [entry for entry in entries if _glob_matches(entry, base_path=base_path, pattern=pattern)]
    return matches, gitignore_applied


def _iter_matching_files(
    base_path: Path,
    *,
    glob_pattern: str | None,
    include_hidden: bool,
    respect_gitignore: bool,
    exclude_patterns: tuple[str, ...],
) -> tuple[list[Path], bool]:
    pattern = glob_pattern or "*"
    entries, gitignore_applied = _iter_matching_entries(
        base_path,
        pattern=pattern,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=exclude_patterns,
    )
    return [path for path in entries if path.is_file()], gitignore_applied


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:1024]:
        return None
    return raw.decode("utf-8", errors="replace")


def glob_files(
    base_path: Path,
    *,
    pattern: str,
    limit: int,
    offset: int,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    validation_error = _validate_existing_path(base_path)
    if validation_error:
        return validation_error

    matches, gitignore_applied = _iter_matching_entries(
        base_path,
        pattern=pattern,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=tuple(exclude_patterns or ()),
    )
    payload = [
        {
            "path": str(path),
            "is_dir": path.is_dir(),
        }
        for path in matches
    ]
    selected, truncated, next_offset = _paginate(payload, offset=offset, limit=limit)
    return {
        "success": True,
        "base_path": str(base_path),
        "pattern": pattern,
        "matches": selected,
        "truncated": truncated,
        "next_offset": next_offset,
        "filters": {
            "include_hidden": include_hidden,
            "respect_gitignore": respect_gitignore,
            "gitignore_applied": gitignore_applied,
            "exclude_patterns": list(exclude_patterns or ()),
        },
    }


def grep_files(
    base_path: Path,
    *,
    pattern: str,
    glob_pattern: str | None,
    output_mode: str,
    before: int = 0,
    after: int = 0,
    ignore_case: bool = False,
    head_limit: int,
    offset: int,
    multiline: bool = False,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    validation_error = _validate_existing_path(base_path)
    if validation_error:
        return validation_error

    if output_mode not in {"content", "files_with_matches", "count"}:
        return _error("invalid_output_mode", f"Unsupported output_mode: {output_mode}")

    flags = re.MULTILINE
    if ignore_case:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL

    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return _error("invalid_pattern", str(exc), pattern=pattern)

    matches_exclude = tuple(exclude_patterns or ())
    candidate_files, gitignore_applied = _iter_matching_files(
        base_path,
        glob_pattern=glob_pattern,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=matches_exclude,
    )

    if output_mode == "files_with_matches":
        files: list[str] = []
        for path in candidate_files:
            content = _read_text(path)
            if content is None:
                continue
            if compiled.search(content):
                files.append(str(path))
        selected, truncated, next_offset = _paginate(files, offset=offset, limit=head_limit)
        return {
            "success": True,
            "base_path": str(base_path),
            "pattern": pattern,
            "output_mode": output_mode,
            "files": selected,
            "truncated": truncated,
            "next_offset": next_offset,
            "filters": {
                "include_hidden": include_hidden,
                "respect_gitignore": respect_gitignore,
                "gitignore_applied": gitignore_applied,
                "exclude_patterns": list(matches_exclude),
            },
        }

    if output_mode == "count":
        counts: list[dict[str, object]] = []
        for path in candidate_files:
            content = _read_text(path)
            if content is None:
                continue
            count = sum(1 for _ in compiled.finditer(content))
            if count:
                counts.append({"path": str(path), "count": count})
        selected, truncated, next_offset = _paginate(counts, offset=offset, limit=head_limit)
        return {
            "success": True,
            "base_path": str(base_path),
            "pattern": pattern,
            "output_mode": output_mode,
            "counts": selected,
            "truncated": truncated,
            "next_offset": next_offset,
            "filters": {
                "include_hidden": include_hidden,
                "respect_gitignore": respect_gitignore,
                "gitignore_applied": gitignore_applied,
                "exclude_patterns": list(matches_exclude),
            },
        }

    matches: list[dict[str, object]] = []
    for path in candidate_files:
        content = _read_text(path)
        if content is None:
            continue
        lines = content.splitlines()
        if multiline:
            for match in compiled.finditer(content):
                start_line = content.count("\n", 0, match.start()) + 1
                end_line = content.count("\n", 0, match.end()) + 1
                matches.append(
                    {
                        "path": str(path),
                        "line_number": start_line,
                        "end_line_number": end_line,
                        "line": match.group(0),
                        "context_before": lines[max(start_line - 1 - before, 0) : start_line - 1],
                        "context_after": lines[end_line : end_line + after],
                    }
                )
        else:
            for line_number, line in enumerate(lines, start=1):
                if not compiled.search(line):
                    continue
                matches.append(
                    {
                        "path": str(path),
                        "line_number": line_number,
                        "line": line,
                        "context_before": lines[max(line_number - 1 - before, 0) : line_number - 1],
                        "context_after": lines[line_number : line_number + after],
                    }
                )

    selected, truncated, next_offset = _paginate(matches, offset=offset, limit=head_limit)
    return {
        "success": True,
        "base_path": str(base_path),
        "pattern": pattern,
        "output_mode": output_mode,
        "matches": selected,
        "truncated": truncated,
        "next_offset": next_offset,
        "filters": {
            "include_hidden": include_hidden,
            "respect_gitignore": respect_gitignore,
            "gitignore_applied": gitignore_applied,
            "exclude_patterns": list(matches_exclude),
        },
    }


def search_files(
    base_path: Path,
    *,
    query: str,
    glob_pattern: str | None,
    limit: int,
    include_hidden: bool = False,
    respect_gitignore: bool = True,
    exclude_patterns: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    result = grep_files(
        base_path,
        pattern=re.escape(query),
        glob_pattern=glob_pattern,
        output_mode="content",
        before=0,
        after=0,
        ignore_case=False,
        head_limit=limit,
        offset=0,
        multiline=False,
        include_hidden=include_hidden,
        respect_gitignore=respect_gitignore,
        exclude_patterns=exclude_patterns,
    )
    if not result["success"]:
        return result
    return {
        "success": True,
        "matches": [
            {
                "path": match["path"],
                "line_number": match["line_number"],
                "line": match["line"],
            }
            for match in result["matches"]
        ],
        "truncated": result["truncated"],
        "filters": result["filters"],
    }
