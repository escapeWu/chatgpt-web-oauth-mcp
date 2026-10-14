from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from .tool_context import LOCAL_STATE_TOOL

DEFAULT_SCRIPTS_DIR = Path("/home/shancw/.codex/skills/browser-use-plm-entry-web/scripts")
SCRIPTS_ENV = "CHATGPT_MCP_PLM_WEB_SCRIPTS"

CONTEXT_FIELD = Field(description="Round context directory from the task message (CTX=...), under planner-contexts.")
SYMBOL_FIELD = Field(description="The single symbol assigned to this conversation, e.g. ALABUSDT.")


def _load_web_compile() -> Any:
    # Loaded per call so a skill update takes effect without restarting the server.
    path = Path(os.environ.get(SCRIPTS_ENV) or DEFAULT_SCRIPTS_DIR) / "web_compile.py"
    spec = importlib.util.spec_from_file_location("plm_web_compile", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise FileNotFoundError(str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(command: str, context: str, symbol: str, **kwargs: Any) -> dict[str, Any]:
    try:
        module = _load_web_compile()
    except Exception as exc:  # noqa: BLE001 - report instead of failing the MCP call
        return {"ok": False, "error": f"PLM_WEB_TOOLS_UNAVAILABLE: {type(exc).__name__}: {exc}"[:500]}
    return module.execute(command, context, symbol, **kwargs)


def register_plm_web_tools(mcp: Any) -> dict[str, object]:
    """Two narrow tools for the PLM web analyst; they replace a general command runner."""

    @mcp.tool(
        name="plm_skeleton",
        title="PLM Decision Skeleton",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Return the decision skeleton for one assigned symbol of a PLM web round: every derivable field "
            "is prefilled and only the judgment fields are left to fill. Writes only decision.skeleton.json "
            "inside that symbol's round folder; rejects anything outside an assigned PLM web round."
        ),
    )
    def plm_skeleton(
        context: Annotated[str, CONTEXT_FIELD],
        symbol: Annotated[str, SYMBOL_FIELD],
        result: Annotated[
            Literal["KEEP", "UPDATE", "INVALIDATE", "INITIAL", "BLOCKED"],
            Field(description="The judgment result the skeleton is for."),
        ],
    ) -> dict[str, Any]:
        return _run("skeleton", context, symbol, result=result)

    @mcp.tool(
        name="plm_check",
        title="PLM Draft Check",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Validate a draft decision (and BattleMap patch or full BattleMap) for one assigned symbol with the "
            "read-only planner check. Saves the draft files inside that symbol's round folder, overwriting your "
            "previous attempt, and returns ok plus draft_sha256 or the exact errors to fix. Can be called again "
            "after fixing. It does not publish, commit or deploy anything."
        ),
    )
    def plm_check(
        context: Annotated[str, CONTEXT_FIELD],
        symbol: Annotated[str, SYMBOL_FIELD],
        decision: Annotated[dict[str, Any], Field(description="The complete decision object.")],
        battlemap_patch: Annotated[
            dict[str, Any] | None, Field(description="UPDATE only: the BattleMap patch; otherwise omit or null.")
        ] = None,
        battlemap: Annotated[
            dict[str, Any] | None, Field(description="INITIAL only: the full BattleMap; otherwise omit or null.")
        ] = None,
    ) -> dict[str, Any]:
        bundle = {"decision": decision, "battlemap_patch": battlemap_patch, "battlemap": battlemap}
        return _run("check", context, symbol, bundle=bundle)

    return {"plm_skeleton": plm_skeleton, "plm_check": plm_check}
