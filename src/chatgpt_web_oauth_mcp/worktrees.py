from __future__ import annotations

import re
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


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )


def _check_cwd(cwd: Path) -> dict[str, object] | None:
    if not cwd.exists():
        return _error("cwd_not_found", f"Working directory not found: {cwd}", cwd=str(cwd))
    if not cwd.is_dir():
        return _error("cwd_not_directory", f"Working directory is not a directory: {cwd}", cwd=str(cwd))
    return None


def repo_root(cwd: Path) -> dict[str, object]:
    cwd_error = _check_cwd(cwd)
    if cwd_error:
        return cwd_error
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if result.returncode != 0:
        return _error(
            "not_a_git_repo",
            "Working directory is not inside a git repository.",
            cwd=str(cwd),
            stderr=result.stderr.strip(),
        )
    return {"success": True, "repo_root": result.stdout.strip()}


def rev_parse(cwd: Path, ref: str) -> dict[str, object]:
    cwd_error = _check_cwd(cwd)
    if cwd_error:
        return cwd_error
    result = _run_git(["rev-parse", ref], cwd=cwd)
    if result.returncode != 0:
        return _error(
            "git_rev_parse_failed",
            result.stderr.strip() or f"Could not resolve git ref {ref!r}.",
            cwd=str(cwd),
            ref=ref,
        )
    return {"success": True, "ref": ref, "sha": result.stdout.strip()}


_BRANCH_SAFE_RE = re.compile(r"[^A-Za-z0-9._/-]+")


def safe_branch_name(prefix: str, board_id: str, task_id: str) -> str:
    clean_prefix = _BRANCH_SAFE_RE.sub("-", (prefix or "taskboard").strip()).strip("/.-")
    if not clean_prefix:
        clean_prefix = "taskboard"
    clean_board = _BRANCH_SAFE_RE.sub("-", board_id).strip("/.-") or "board"
    clean_task = _BRANCH_SAFE_RE.sub("-", task_id).strip("/.-") or "task"
    return f"{clean_prefix}/{clean_board}-{clean_task}"


def create_worktree(
    *,
    repo_cwd: Path,
    worktree_path: Path,
    branch_name: str,
    base_ref: str,
) -> dict[str, object]:
    root_result = repo_root(repo_cwd)
    if not root_result.get("success"):
        return root_result

    base_result = rev_parse(repo_cwd, base_ref)
    if not base_result.get("success"):
        return base_result
    base_sha = str(base_result["sha"])

    if worktree_path.exists():
        existing = repo_root(worktree_path)
        if existing.get("success"):
            head = rev_parse(worktree_path, "HEAD")
            return {
                "success": True,
                "repo_root": root_result["repo_root"],
                "worktree_path": str(worktree_path),
                "branch_name": branch_name,
                "base_ref": base_ref,
                "base_sha": base_sha,
                "head_sha": head.get("sha") if head.get("success") else None,
                "reused": True,
            }
        return _error(
            "worktree_path_exists",
            f"Worktree path already exists and is not a git worktree: {worktree_path}",
            worktree_path=str(worktree_path),
        )

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(
        ["worktree", "add", "-b", branch_name, str(worktree_path), base_ref],
        cwd=repo_cwd,
    )
    if result.returncode != 0:
        return _error(
            "git_worktree_add_failed",
            result.stderr.strip() or "git worktree add failed.",
            cwd=str(repo_cwd),
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            base_ref=base_ref,
        )

    head = rev_parse(worktree_path, "HEAD")
    return {
        "success": True,
        "repo_root": root_result["repo_root"],
        "worktree_path": str(worktree_path),
        "branch_name": branch_name,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "head_sha": head.get("sha") if head.get("success") else base_sha,
        "reused": False,
    }


def _cap_text(text: str, max_bytes: int) -> tuple[str, bool, int]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False, len(encoded)
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True, len(encoded)


def _changed_files(cwd: Path, base_sha: str | None) -> list[str]:
    files: list[str] = []

    def add_lines(result: subprocess.CompletedProcess[str]) -> None:
        if result.returncode != 0:
            return
        for line in result.stdout.splitlines():
            if line and line not in files:
                files.append(line)

    if base_sha:
        add_lines(_run_git(["diff", "--name-only", base_sha, "--"], cwd=cwd))
    add_lines(_run_git(["diff", "--cached", "--name-only", "--"], cwd=cwd))
    add_lines(_run_git(["ls-files", "--others", "--exclude-standard"], cwd=cwd))
    return files


def collect_result(
    *,
    worktree_path: Path,
    base_sha: str | None,
    include_diff: bool = False,
    diff_max_bytes: int = 20000,
    include_log_tail: bool = False,
    log_tail_chars: int = 4000,
) -> dict[str, object]:
    cwd_error = _check_cwd(worktree_path)
    if cwd_error:
        return cwd_error

    root_result = repo_root(worktree_path)
    if not root_result.get("success"):
        return root_result

    head = rev_parse(worktree_path, "HEAD")
    head_sha = str(head.get("sha") or "") if head.get("success") else None
    commit_sha = head_sha if base_sha and head_sha and head_sha != base_sha else None

    status_result = _run_git(["status", "--short"], cwd=worktree_path)
    status = status_result.stdout if status_result.returncode == 0 else ""

    diff_summary = ""
    diff_summary_truncated = False
    diff_summary_bytes = 0
    if base_sha:
        summary_result = _run_git(["diff", "--stat", base_sha, "--"], cwd=worktree_path)
        if summary_result.returncode == 0:
            diff_summary, diff_summary_truncated, diff_summary_bytes = _cap_text(summary_result.stdout, 4000)

    payload: dict[str, object] = {
        "success": True,
        "worktree_path": str(worktree_path),
        "repo_root": root_result["repo_root"],
        "base_sha": base_sha,
        "head_sha": head_sha,
        "commit_sha": commit_sha,
        "changed_files": _changed_files(worktree_path, base_sha),
        "status": status,
        "diff_summary": diff_summary,
        "diff_summary_truncated": diff_summary_truncated,
        "diff_summary_bytes": diff_summary_bytes,
    }

    if include_diff:
        diff_result = (
            _run_git(["diff", "--no-color", base_sha, "--"], cwd=worktree_path)
            if base_sha
            else _run_git(["diff", "--no-color"], cwd=worktree_path)
        )
        diff_text = diff_result.stdout if diff_result.returncode == 0 else diff_result.stderr
        diff, truncated, total_bytes = _cap_text(diff_text, max(int(diff_max_bytes), 0))
        payload.update(
            {
                "diff": diff,
                "diff_truncated": truncated,
                "diff_total_bytes": total_bytes,
            }
        )

    if include_log_tail:
        log_result = _run_git(["log", "--oneline", "--decorate", "--max-count=20"], cwd=worktree_path)
        log_text = log_result.stdout if log_result.returncode == 0 else log_result.stderr
        payload["log_tail"] = log_text[-max(int(log_tail_chars), 0) :]

    return payload
