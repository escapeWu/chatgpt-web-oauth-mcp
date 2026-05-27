from __future__ import annotations

import os


# The developer workstation may have a real .env / launchd environment for the
# live ChatGPT connector. Unit tests must start from deterministic defaults and
# should opt into auth modes explicitly via monkeypatch/module attributes.
_NOTEBOOKLM_E2E_ENABLED = os.environ.get("NOTEBOOKLM_E2E", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_NOTEBOOKLM_E2E_ENV = {
    "NOTEBOOKLM_E2E",
    "NOTEBOOKLM_E2E_NOTEBOOK_ID",
    "NOTEBOOKLM_E2E_CREATE_TEMP",
    "NOTEBOOKLM_STORAGE_PATH",
    "NOTEBOOKLM_PROFILE",
    "NOTEBOOKLM_TIMEOUT_SECONDS",
    "CHATGPT_MCP_NOTEBOOKLM_DEFAULT_NOTEBOOK_ID",
    "NOTEBOOKLM_NOTEBOOK",
}
for key in list(os.environ):
    if (
        key.startswith("CHATGPT_MCP_")
        or key.startswith("OBSIDIAN_")
        or key.startswith("NOTEBOOKLM_")
        or key.startswith("TG_")
    ):
        if _NOTEBOOKLM_E2E_ENABLED and key in _NOTEBOOKLM_E2E_ENV:
            continue
        os.environ.pop(key, None)
