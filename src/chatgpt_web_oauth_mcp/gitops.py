from __future__ import annotations

import subprocess
from pathlib import Path


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


def _cwd_error(cwd: Path) -> dict[str, object] | None:
    if not cwd.exists():
        return _error("cwd_not_found", f"Working directory not found: {cwd}", cwd=str(cwd))
    if not cwd.is_dir():
        return _error("cwd_not_directory", f"Working directory is not a directory: {cwd}", cwd=str(cwd))
    return None


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )


def _require_repo(cwd: Path) -> tuple[Path, str] | dict[str, object]:
    cwd_error = _cwd_error(cwd)
    if cwd_error:
        return cwd_error

    root_result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if root_result.returncode != 0:
        return _error(
            "not_a_git_repo",
            "Working directory is not inside a git repository.",
            cwd=str(cwd),
            stderr=root_result.stderr.strip(),
        )

    branch_result = _run_git(["branch", "--show-current"], cwd=cwd)
    branch = branch_result.stdout.strip() or "HEAD"
    return Path(root_result.stdout.strip()), branch


def _normalize_pathspec(pathspec: str, *, cwd: Path, repo_root: Path) -> str:
    raw = Path(pathspec).expanduser()
    absolute = (cwd / raw).resolve(strict=False) if not raw.is_absolute() else raw.resolve(strict=False)
    try:
        return str(absolute.relative_to(repo_root))
    except ValueError:
        return pathspec


def git_status(*, cwd: Path) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, branch = repo_info

    result = _run_git(["status", "--short", "--branch"], cwd=cwd)
    if result.returncode != 0:
        return _error("git_status_failed", result.stderr.strip() or "git status failed.", cwd=str(cwd))

    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    entries: list[dict[str, object]] = []

    for line in result.stdout.splitlines():
        if line.startswith("## "):
            continue
        code = line[:2]
        raw_path = line[3:]
        path = raw_path.split(" -> ", 1)[-1]
        entries.append(
            {
                "path": path,
                "index_status": code[0],
                "worktree_status": code[1],
            }
        )
        if code == "??":
            untracked.append(path)
            continue
        if code[0] != " ":
            staged.append(path)
        if code[1] != " ":
            unstaged.append(path)

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "branch": branch,
        "clean": not entries,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "entries": entries,
    }


_DIFF_FILE_HEADER = "diff --git "


def _split_diff_by_file(diff_text: str) -> list[tuple[str, str]]:
    """Split a combined unified diff into ``(path, per_file_diff)`` tuples.

    The path is derived from the ``+++ b/<path>`` marker when present; falling
    back to the ``diff --git`` header. Binary-file stanzas and deletions still
    work because we lean on the git-provided headers.
    """
    if not diff_text:
        return []
    chunks: list[tuple[str, str]] = []
    buffer: list[str] = []
    current_path = ""

    def flush() -> None:
        if not buffer:
            return
        chunks.append((current_path or "(unknown)", "".join(buffer)))
        buffer.clear()

    for raw_line in diff_text.splitlines(keepends=True):
        if raw_line.startswith(_DIFF_FILE_HEADER):
            flush()
            header = raw_line[len(_DIFF_FILE_HEADER) :].rstrip("\n")
            parts = header.split(" ")
            # headers look like `a/<path> b/<path>`; fall back to last token.
            candidate = parts[-1] if parts else ""
            current_path = candidate[2:] if candidate.startswith("b/") else candidate
        elif raw_line.startswith("+++ ") and buffer:
            marker = raw_line[4:].rstrip("\n")
            if marker.startswith("b/"):
                current_path = marker[2:]
            elif marker != "/dev/null":
                current_path = marker
        buffer.append(raw_line)
    flush()
    return chunks


def git_diff(
    *,
    cwd: Path,
    staged: bool = False,
    paths: list[str] | None = None,
    max_bytes: int = 65536,
    per_file_max_bytes: int = 16384,
) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info
    normalized_paths = [_normalize_pathspec(path, cwd=cwd, repo_root=repo_root) for path in (paths or [])]

    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if normalized_paths:
        args.extend(["--", *normalized_paths])

    result = _run_git(args, cwd=cwd)
    if result.returncode != 0:
        return _error("git_diff_failed", result.stderr.strip() or "git diff failed.", cwd=str(cwd))

    # numstat gives added/removed line counts per file (binary => "-").
    numstat_args = ["diff", "--numstat"]
    if staged:
        numstat_args.append("--cached")
    if normalized_paths:
        numstat_args.extend(["--", *normalized_paths])
    numstat = _run_git(numstat_args, cwd=cwd)
    stats: dict[str, dict[str, object]] = {}
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_raw, removed_raw, path = parts
        stats[path] = {
            "added": None if added_raw == "-" else int(added_raw),
            "removed": None if removed_raw == "-" else int(removed_raw),
            "binary": added_raw == "-" and removed_raw == "-",
        }

    files_payload: list[dict[str, object]] = []
    for path, chunk in _split_diff_by_file(result.stdout):
        encoded_chunk = chunk.encode("utf-8")
        chunk_truncated = len(encoded_chunk) > per_file_max_bytes
        rendered = (
            encoded_chunk[:per_file_max_bytes].decode("utf-8", errors="ignore")
            if chunk_truncated
            else chunk
        )
        files_payload.append(
            {
                "path": path,
                "diff": rendered,
                "truncated": chunk_truncated,
                "bytes": len(encoded_chunk),
                **stats.get(path, {"added": None, "removed": None, "binary": False}),
            }
        )

    # Keep the flat `diff` + `files` names for back-compat; add richer payload.
    encoded = result.stdout.encode("utf-8")
    truncated = len(encoded) > max_bytes
    diff_text = encoded[:max_bytes].decode("utf-8", errors="ignore") if truncated else result.stdout

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "staged": staged,
        "files": [entry["path"] for entry in files_payload],
        "file_diffs": files_payload,
        "diff": diff_text,
        "truncated": truncated,
        "total_bytes": len(encoded),
    }


def git_commit(
    *,
    cwd: Path,
    message: str,
    paths: list[str] | None = None,
    stage_all: bool = False,
    amend: bool = False,
    allow_empty: bool = False,
    author: str | None = None,
    sign_off: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, branch = repo_info
    normalized_paths = [_normalize_pathspec(path, cwd=cwd, repo_root=repo_root) for path in (paths or [])]

    if stage_all:
        if not dry_run:
            stage_result = _run_git(["add", "-A"], cwd=cwd)
            if stage_result.returncode != 0:
                return _error("git_add_failed", stage_result.stderr.strip() or "git add -A failed.", cwd=str(cwd))
    elif normalized_paths:
        if not dry_run:
            stage_result = _run_git(["add", "--", *normalized_paths], cwd=cwd)
            if stage_result.returncode != 0:
                return _error("git_add_failed", stage_result.stderr.strip() or "git add failed.", cwd=str(cwd))

    staged_result = _run_git(["diff", "--cached", "--name-only"], cwd=cwd)
    staged_files = [line for line in staged_result.stdout.splitlines() if line]
    would_stage_files: list[str] = []
    if stage_all:
        pending = _run_git(["status", "--porcelain"], cwd=cwd)
        would_stage_files = [line[3:].split(" -> ", 1)[-1] for line in pending.stdout.splitlines() if line]
    elif normalized_paths:
        would_stage_files = normalized_paths

    effective_files = staged_files + [item for item in would_stage_files if item not in staged_files]

    if not effective_files and not allow_empty and not amend:
        return _error("nothing_to_commit", "No staged changes to commit.", cwd=str(cwd))

    commit_args = ["commit", "-m", message]
    if amend:
        commit_args.append("--amend")
    if allow_empty:
        commit_args.append("--allow-empty")
    if author:
        commit_args.extend(["--author", author])
    if sign_off:
        commit_args.append("--signoff")

    if dry_run:
        return {
            "success": True,
            "cwd": str(cwd),
            "repo_root": str(repo_root),
            "branch": branch,
            "summary": message,
            "files": effective_files,
            "amended": amend,
            "allow_empty": allow_empty,
            "dry_run": True,
            "would_stage": would_stage_files,
            "commit_args": commit_args,
        }

    commit_result = _run_git(commit_args, cwd=cwd)
    if commit_result.returncode != 0:
        return _error(
            "git_commit_failed",
            commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit failed.",
            cwd=str(cwd),
        )

    head_result = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    commit_hash = head_result.stdout.strip()
    # For amend, re-read the resulting staged file list so callers see what is
    # actually in the new commit.
    if amend:
        changed = _run_git(
            ["show", "--name-only", "--pretty=format:", commit_hash],
            cwd=cwd,
        )
        committed_files = [line for line in changed.stdout.splitlines() if line]
    else:
        committed_files = staged_files
    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "branch": branch,
        "commit": commit_hash,
        "short_commit": commit_hash[:7],
        "summary": message,
        "files": committed_files,
        "amended": amend,
        "allow_empty": allow_empty,
        "dry_run": False,
    }


def git_log(*, cwd: Path, limit: int = 10) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, branch = repo_info

    result = _run_git(
        ["log", f"--max-count={max(limit, 1)}", "--pretty=format:%H%x1f%h%x1f%s%x1f%an%x1f%aI"],
        cwd=cwd,
    )
    if result.returncode != 0:
        return _error("git_log_failed", result.stderr.strip() or "git log failed.", cwd=str(cwd))

    entries: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        commit, short_commit, summary, author, committed_at = line.split("\x1f")
        entries.append(
            {
                "commit": commit,
                "short_commit": short_commit,
                "summary": summary,
                "author": author,
                "committed_at": committed_at,
            }
        )

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "branch": branch,
        "entries": entries,
    }


def git_show(
    *,
    cwd: Path,
    ref: str = "HEAD",
    max_bytes: int = 65536,
    per_file_max_bytes: int = 16384,
) -> dict[str, object]:
    """Return metadata + diff for a single commit (or any git ref)."""
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info

    meta = _run_git(
        [
            "show",
            "--no-color",
            "--no-patch",
            "--pretty=format:%H%x1f%h%x1f%s%x1f%an%x1f%aI%x1f%P%x1f%B",
            ref,
        ],
        cwd=cwd,
    )
    if meta.returncode != 0:
        return _error(
            "git_show_failed",
            meta.stderr.strip() or f"git show failed for ref {ref!r}.",
            cwd=str(cwd),
            ref=ref,
        )
    parts = meta.stdout.split("\x1f", 6)
    if len(parts) < 7:
        return _error("git_show_failed", "Unexpected git show output.", cwd=str(cwd), ref=ref)
    commit, short_commit, summary, author, committed_at, parents_raw, body = parts
    body = body.rstrip("\n")
    parents = [p for p in parents_raw.split(" ") if p]

    diff_result = _run_git(["show", "--no-color", "--format=", ref], cwd=cwd)
    if diff_result.returncode != 0:
        return _error(
            "git_show_failed",
            diff_result.stderr.strip() or f"git show diff failed for ref {ref!r}.",
            cwd=str(cwd),
            ref=ref,
        )
    file_diffs: list[dict[str, object]] = []
    for path, chunk in _split_diff_by_file(diff_result.stdout):
        encoded_chunk = chunk.encode("utf-8")
        chunk_truncated = len(encoded_chunk) > per_file_max_bytes
        rendered = (
            encoded_chunk[:per_file_max_bytes].decode("utf-8", errors="ignore")
            if chunk_truncated
            else chunk
        )
        file_diffs.append(
            {
                "path": path,
                "diff": rendered,
                "truncated": chunk_truncated,
                "bytes": len(encoded_chunk),
            }
        )

    encoded = diff_result.stdout.encode("utf-8")
    truncated = len(encoded) > max_bytes
    diff_text = encoded[:max_bytes].decode("utf-8", errors="ignore") if truncated else diff_result.stdout

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "ref": ref,
        "commit": commit,
        "short_commit": short_commit,
        "summary": summary,
        "author": author,
        "committed_at": committed_at,
        "parents": parents,
        "body": body,
        "files": [entry["path"] for entry in file_diffs],
        "file_diffs": file_diffs,
        "diff": diff_text,
        "truncated": truncated,
        "total_bytes": len(encoded),
    }


def git_blame(
    *,
    cwd: Path,
    path: str,
    ref: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, object]:
    """Return per-line blame info for a file."""
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info

    normalized = _normalize_pathspec(path, cwd=cwd, repo_root=repo_root)
    args = ["blame", "--porcelain"]
    if start_line is not None or end_line is not None:
        a = max(start_line or 1, 1)
        b = end_line if end_line is not None else a
        args.extend(["-L", f"{a},{b}"])
    if ref:
        args.append(ref)
    args.extend(["--", normalized])

    result = _run_git(args, cwd=cwd)
    if result.returncode != 0:
        return _error(
            "git_blame_failed",
            result.stderr.strip() or "git blame failed.",
            cwd=str(cwd),
            path=path,
            ref=ref,
        )

    # Minimal porcelain parser: each line entry starts with `<sha> <orig_line>
    # <final_line> [<group_size>]`, followed by zero or more header lines
    # (author, summary, ...) and exactly one content line prefixed with a tab.
    entries: list[dict[str, object]] = []
    meta: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in result.stdout.splitlines():
        if current is None:
            header = raw.split(" ")
            if len(header) < 3:
                continue
            sha = header[0]
            final_line = int(header[2])
            current = meta.setdefault(sha, {})
            current["__sha__"] = sha
            current["__final_line__"] = str(final_line)
            continue
        if raw.startswith("\t"):
            sha = current.get("__sha__", "")
            final_line = int(current.get("__final_line__", "0"))
            info = meta.get(sha, {})
            entries.append(
                {
                    "line": final_line,
                    "commit": sha,
                    "short_commit": sha[:7],
                    "author": info.get("author", ""),
                    "author_time": info.get("author-time", ""),
                    "summary": info.get("summary", ""),
                    "content": raw[1:],
                }
            )
            current = None
            continue
        key, _, value = raw.partition(" ")
        if key in {"author", "author-time", "summary", "filename", "previous"}:
            current[key] = value

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "path": normalized,
        "ref": ref,
        "entries": entries,
    }


def _resolve_worktree_path(path: str, *, cwd: Path) -> Path:
    raw = Path(path).expanduser()
    if raw.is_absolute():
        return raw.resolve(strict=False)
    return (cwd / raw).resolve(strict=False)


def _parse_worktree_list(output: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    current: dict[str, object] = {}

    def flush() -> None:
        nonlocal current
        if current:
            current.setdefault("branch", None)
            current.setdefault("branch_ref", None)
            current.setdefault("commit", None)
            current.setdefault("detached", False)
            current.setdefault("bare", False)
            current.setdefault("locked", False)
            current.setdefault("prunable", False)
            entries.append(current)
            current = {}

    for line in output.splitlines():
        if not line:
            flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            flush()
            current["path"] = value
            continue
        if key == "HEAD":
            current["commit"] = value
            continue
        if key == "branch":
            current["branch_ref"] = value
            current["branch"] = value.removeprefix("refs/heads/")
            continue
        if key in {"detached", "bare", "locked", "prunable"}:
            current[key] = True
            if value:
                current[f"{key}_reason"] = value
            continue
        if key:
            current[key] = value or True
    flush()
    return entries


def _worktree_list_payload(*, cwd: Path, repo_root: Path) -> dict[str, object]:
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        return _error(
            "git_worktree_list_failed",
            result.stderr.strip() or "git worktree list failed.",
            cwd=str(cwd),
        )
    worktrees = _parse_worktree_list(result.stdout)
    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "worktrees": worktrees,
    }


def _find_worktree(entries: list[dict[str, object]], target: Path) -> dict[str, object] | None:
    target_resolved = target.resolve(strict=False)
    for entry in entries:
        path_value = entry.get("path")
        if not isinstance(path_value, str):
            continue
        if Path(path_value).resolve(strict=False) == target_resolved:
            return entry
    return None


def git_worktree_list(*, cwd: Path) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info
    return _worktree_list_payload(cwd=cwd, repo_root=repo_root)


def git_worktree_create(
    *,
    cwd: Path,
    path: str,
    base_ref: str = "HEAD",
    mode: str = "clean",
    branch: str | None = None,
) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info

    if mode not in {"clean", "detached"}:
        return _error(
            "unsupported_worktree_mode",
            "git_worktree_create supports mode='clean' and mode='detached' only.",
            cwd=str(cwd),
            mode=mode,
        )
    if mode == "detached" and branch:
        return _error(
            "invalid_arguments",
            "branch can only be used with mode='clean'.",
            cwd=str(cwd),
            mode=mode,
            branch=branch,
        )

    target = _resolve_worktree_path(path, cwd=cwd)
    args = ["worktree", "add"]
    branch_name: str | None = None
    if mode == "detached":
        args.append("--detach")
    else:
        branch_name = branch or target.name
        args.extend(["-b", branch_name])
    args.extend([str(target), base_ref])

    result = _run_git(args, cwd=repo_root)
    if result.returncode != 0:
        return _error(
            "git_worktree_create_failed",
            result.stderr.strip() or result.stdout.strip() or "git worktree add failed.",
            cwd=str(cwd),
            repo_root=str(repo_root),
            path=str(target),
            base_ref=base_ref,
            mode=mode,
            branch=branch_name,
        )

    list_result = _worktree_list_payload(cwd=cwd, repo_root=repo_root)
    entry = None
    if list_result.get("success") is True:
        entry = _find_worktree(list_result["worktrees"], target)  # type: ignore[arg-type]

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "path": str(target),
        "base_ref": base_ref,
        "mode": mode,
        "branch": branch_name,
        "worktree": entry,
    }


def _worktree_status_entry(entry: dict[str, object]) -> dict[str, object]:
    path_value = entry.get("path")
    if not isinstance(path_value, str):
        return {**entry, "status": _error("worktree_path_missing", "Worktree entry has no path.")}
    path = Path(path_value)
    if entry.get("bare") is True:
        return {**entry, "status": None, "clean": None}
    status = git_status(cwd=path)
    payload = {**entry, "status": status}
    if status.get("success") is True:
        payload["clean"] = status.get("clean")
    return payload


def git_worktree_status(*, cwd: Path, path: str | None = None) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info

    list_result = _worktree_list_payload(cwd=cwd, repo_root=repo_root)
    if list_result.get("success") is not True:
        return list_result
    entries = list_result["worktrees"]  # type: ignore[assignment]

    if path is None:
        statuses = [_worktree_status_entry(entry) for entry in entries]  # type: ignore[arg-type]
        return {
            "success": True,
            "cwd": str(cwd),
            "repo_root": str(repo_root),
            "worktrees": statuses,
        }

    target = _resolve_worktree_path(path, cwd=cwd)
    entry = _find_worktree(entries, target)  # type: ignore[arg-type]
    if entry is None:
        return _error(
            "worktree_not_found",
            "Path is not a registered git worktree.",
            cwd=str(cwd),
            repo_root=str(repo_root),
            path=str(target),
        )

    status_entry = _worktree_status_entry(entry)
    status = status_entry.get("status")
    if not isinstance(status, dict) or status.get("success") is not True:
        return _error(
            "git_worktree_status_failed",
            "Could not read git status for worktree.",
            cwd=str(cwd),
            repo_root=str(repo_root),
            path=str(target),
            status=status,
        )
    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "path": str(target),
        "worktree": entry,
        "status": status,
        "clean": status.get("clean"),
    }


def git_worktree_remove(*, cwd: Path, path: str, force: bool = False) -> dict[str, object]:
    repo_info = _require_repo(cwd)
    if isinstance(repo_info, dict):
        return repo_info
    repo_root, _branch = repo_info

    target = _resolve_worktree_path(path, cwd=cwd)
    list_result = _worktree_list_payload(cwd=cwd, repo_root=repo_root)
    if list_result.get("success") is not True:
        return list_result
    entry = _find_worktree(list_result["worktrees"], target)  # type: ignore[arg-type]
    if entry is None:
        return _error(
            "worktree_not_found",
            "Path is not a registered git worktree.",
            cwd=str(cwd),
            repo_root=str(repo_root),
            path=str(target),
        )

    status_result = git_status(cwd=target)
    if status_result.get("success") is not True:
        return _error(
            "git_worktree_status_failed",
            "Could not read git status for worktree.",
            cwd=str(cwd),
            repo_root=str(repo_root),
            path=str(target),
            status=status_result,
        )
    if not force and status_result.get("clean") is not True:
        return _error(
            "worktree_dirty",
            "Worktree has uncommitted changes; pass force=true to remove it.",
            cwd=str(cwd),
            repo_root=str(repo_root),
            path=str(target),
            status=status_result,
        )

    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    result = _run_git(args, cwd=repo_root)
    if result.returncode != 0:
        return _error(
            "git_worktree_remove_failed",
            result.stderr.strip() or result.stdout.strip() or "git worktree remove failed.",
            cwd=str(cwd),
            repo_root=str(repo_root),
            path=str(target),
            force=force,
        )

    return {
        "success": True,
        "cwd": str(cwd),
        "repo_root": str(repo_root),
        "path": str(target),
        "force": force,
        "removed": True,
    }
