from __future__ import annotations

import subprocess
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_project import ProjectIdentityResolver


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "delegate-tests@example.invalid")
    _git(path, "config", "user.name", "Delegate Tests")
    (path / "README.md").write_text("test\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def test_project_identity_groups_repo_subdirectories_and_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    other_repo = tmp_path / "other"
    worktree = tmp_path / "worktree"
    _init_repo(repo)
    _init_repo(other_repo)
    (repo / "src").mkdir()
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    resolver = ProjectIdentityResolver()

    root_identity = resolver.resolve(repo)
    child_identity = resolver.resolve(repo / "src")
    worktree_identity = resolver.resolve(worktree)
    other_identity = resolver.resolve(other_repo)

    assert root_identity.project_key == child_identity.project_key
    assert root_identity.project_key == worktree_identity.project_key
    assert root_identity.project_key != other_identity.project_key
    assert root_identity.git_common_dir is not None
    assert worktree_identity.project_root == worktree.resolve()


def test_project_identity_falls_back_to_resolved_non_git_cwd(tmp_path: Path) -> None:
    project = tmp_path / "plain"
    project.mkdir()

    identity = ProjectIdentityResolver().resolve(project / ".")

    assert identity.project_key == str(project.resolve())
    assert identity.project_root == project.resolve()
    assert identity.git_common_dir is None
