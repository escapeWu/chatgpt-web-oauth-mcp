from __future__ import annotations

import os


# The developer workstation may have a real .env / launchd environment for the
# live ChatGPT connector. Unit tests must start from deterministic defaults and
# should opt into auth modes explicitly via monkeypatch/module attributes.
for key in list(os.environ):
    if key.startswith("CHATGPT_MCP_"):
        os.environ.pop(key, None)
