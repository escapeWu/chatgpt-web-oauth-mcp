from __future__ import annotations

from collections.abc import Mapping
import os


CONTROL_PLANE_SECRET_ENV_KEYS = frozenset(
    {
        "CHATGPT_MCP_AUTH_TOKEN",
        "CHATGPT_MCP_HEALTH_TOKEN",
        "CHATGPT_MCP_OAUTH_LOGIN_TOKEN",
    }
)


def sanitized_child_env(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return inherited child env without MCP control-plane credentials.

    This is intentionally a narrow deny-list rather than an allow-list: CLI
    providers still need ordinary credentials, proxy settings, PATH, and
    credential-helper environment. The rolling-reload server child is not a
    consumer of this helper because it must inherit the MCP credentials.
    """

    env = os.environ.copy()
    if overrides is not None:
        env.update(overrides)
    for key in CONTROL_PLANE_SECRET_ENV_KEYS:
        env.pop(key, None)
    return env
