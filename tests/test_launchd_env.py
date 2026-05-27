from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_prepare_launchd_env_preserves_notebooklm_shell_overrides(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "launchd-common.sh", scripts_dir / "launchd-common.sh")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "CHATGPT_MCP_ENABLE_NOTEBOOKLM=0",
                "NOTEBOOKLM_STORAGE_PATH=",
                "NOTEBOOKLM_PROFILE=",
                "CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID=",
                "NOTEBOOKLM_NOTEBOOK=",
                "NOTEBOOKLM_TIMEOUT_SECONDS=30",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ.get("PATH", ""),
        "CHATGPT_MCP_ENABLE_NOTEBOOKLM": "1",
        "NOTEBOOKLM_PROFILE": "work",
        "CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID": "nb-shell",
        "NOTEBOOKLM_TIMEOUT_SECONDS": "55",
    }
    script = f"""
        set -euo pipefail
        source {scripts_dir / 'launchd-common.sh'}
        prepare_launchd_env
        test "$CHATGPT_MCP_ENABLE_NOTEBOOKLM" = "1"
        test "$NOTEBOOKLM_PROFILE" = "work"
        test "$CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID" = "nb-shell"
        test "$NOTEBOOKLM_TIMEOUT_SECONDS" = "55"
    """

    subprocess.run(["bash", "-c", script], env=env, check=True)
