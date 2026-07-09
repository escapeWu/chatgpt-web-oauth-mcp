from __future__ import annotations

import subprocess
from pathlib import Path

from chatgpt_web_oauth_mcp.gitops import (
    git_worktree_create,
    git_worktree_list,
    git_worktree_remove,
    git_worktree_status,
)


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True, text=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)


def _entry_by_path(worktrees: list[dict[str, object]], path: Path) -> dict[str, object]:
    target = path.resolve(strict=False)
    for entry in worktrees:
        entry_path = entry.get("path")
        if isinstance(entry_path, str) and Path(entry_path).resolve(strict=False) == target:
            return entry
    raise AssertionError(f"missing worktree entry for {path}")


def test_git_worktree_create_clean_uses_base_ref_even_when_current_repo_is_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    worktree = tmp_path / "feature-wt"
    (repo / "seed.txt").write_text("dirty in main worktree\n", encoding="utf-8")

    result = git_worktree_create(
        cwd=repo,
        path=str(worktree),
        base_ref="HEAD",
        mode="clean",
        branch="feature/worktree",
    )
    listing = git_worktree_list(cwd=repo)

    assert result["success"] is True
    assert result["mode"] == "clean"
    assert result["branch"] == "feature/worktree"
    assert worktree.exists()
    assert (worktree / "seed.txt").read_text(encoding="utf-8") == "seed\n"
    assert listing["success"] is True
    entry = _entry_by_path(listing["worktrees"], worktree)
    assert entry["branch"] == "feature/worktree"
    assert entry["detached"] is False


def test_git_worktree_create_detached_and_status(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    worktree = tmp_path / "detached-wt"

    created = git_worktree_create(cwd=repo, path=str(worktree), base_ref="HEAD", mode="detached")
    status = git_worktree_status(cwd=repo, path=str(worktree))

    assert created["success"] is True
    assert created["mode"] == "detached"
    assert created["branch"] is None
    assert status["success"] is True
    assert status["clean"] is True
    assert status["worktree"]["detached"] is True


def test_git_worktree_remove_clean_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    worktree = tmp_path / "remove-wt"
    git_worktree_create(cwd=repo, path=str(worktree), base_ref="HEAD", mode="clean", branch="remove-wt")

    removed = git_worktree_remove(cwd=repo, path=str(worktree))
    listing = git_worktree_list(cwd=repo)

    assert removed["success"] is True
    assert removed["removed"] is True
    assert not worktree.exists()
    assert listing["success"] is True
    assert all(Path(entry["path"]).resolve(strict=False) != worktree.resolve(strict=False) for entry in listing["worktrees"])


def test_git_worktree_remove_refuses_dirty_worktree_without_force(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    worktree = tmp_path / "dirty-wt"
    git_worktree_create(cwd=repo, path=str(worktree), base_ref="HEAD", mode="detached")
    (worktree / "seed.txt").write_text("changed\n", encoding="utf-8")
    (worktree / "untracked.txt").write_text("new\n", encoding="utf-8")

    status = git_worktree_status(cwd=repo, path=str(worktree))
    refused = git_worktree_remove(cwd=repo, path=str(worktree))

    assert status["success"] is True
    assert status["clean"] is False
    assert "seed.txt" in status["status"]["unstaged"]
    assert "untracked.txt" in status["status"]["untracked"]
    assert refused["success"] is False
    assert refused["error"]["code"] == "worktree_dirty"
    assert worktree.exists()
    forced = git_worktree_remove(cwd=repo, path=str(worktree), force=True)
    assert forced["success"] is True
    assert forced["force"] is True
    assert not worktree.exists()
