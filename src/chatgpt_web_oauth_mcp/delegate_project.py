from __future__ import annotations

import subprocess
from pathlib import Path

from .delegate_models import ProjectIdentity


_ORIGINAL_POPEN = subprocess.Popen


class ProjectIdentityResolver:
    """Resolve a caller cwd to a server-owned, worktree-aware project key."""

    def __init__(self, *, git_binary: str = "git", timeout_seconds: float = 10.0) -> None:
        self.git_binary = git_binary
        self.timeout_seconds = timeout_seconds

    def resolve(self, cwd: Path) -> ProjectIdentity:
        resolved_cwd = cwd.expanduser().resolve(strict=False)
        common_dir = self._rev_parse(
            resolved_cwd,
            "--path-format=absolute",
            "--git-common-dir",
        )
        root = self._rev_parse(resolved_cwd, "--show-toplevel")
        if common_dir and root:
            common_path = Path(common_dir)
            if not common_path.is_absolute():
                common_path = resolved_cwd / common_path
            common_path = common_path.resolve(strict=False)
            root_path = Path(root).resolve(strict=False)
            return ProjectIdentity(
                project_key=str(common_path),
                project_root=root_path,
                git_common_dir=common_path,
            )

        # Older Git versions may not support --path-format. Keep the same
        # worktree-aware identity by resolving --git-common-dir ourselves.
        common_dir = self._rev_parse(resolved_cwd, "--git-common-dir")
        root = root or self._rev_parse(resolved_cwd, "--show-toplevel")
        if common_dir and root:
            common_path = Path(common_dir)
            if not common_path.is_absolute():
                common_path = resolved_cwd / common_path
            common_path = common_path.resolve(strict=False)
            root_path = Path(root).resolve(strict=False)
            return ProjectIdentity(str(common_path), root_path, common_path)

        return ProjectIdentity(
            project_key=str(resolved_cwd),
            project_root=resolved_cwd,
            git_common_dir=None,
        )

    def _rev_parse(self, cwd: Path, *args: str) -> str | None:
        try:
            process = _ORIGINAL_POPEN(
                [self.git_binary, "-C", str(cwd), "rev-parse", *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.communicate()
            except OSError:
                pass
            return None
        except OSError:
            return None
        if process.returncode != 0:
            return None
        value = stdout.strip()
        return value or None
