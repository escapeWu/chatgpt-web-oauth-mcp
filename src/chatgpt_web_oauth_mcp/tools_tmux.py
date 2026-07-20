from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .pathing import resolve_cwd
from .tmux_ops import (
    MAX_CAPTURE_LINES,
    MAX_CAPTURE_OUTPUT_LINES,
    MAX_ENTER_COUNT,
    MAX_KEYS_PER_CALL,
    TmuxClient,
)
from .tool_context import OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


TmuxKey = Literal[
    "Enter",
    "C-m",
    "C-c",
    "C-d",
    "Escape",
    "Tab",
    "BSpace",
    "Delete",
    "Up",
    "Down",
    "Left",
    "Right",
    "Home",
    "End",
    "PageUp",
    "PageDown",
]


def register_tmux_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register small persistent tmux session tools."""

    def client() -> TmuxClient:
        return TmuxClient(
            binary=ctx.tmux_binary,
            socket_name=ctx.tmux_socket_name,
            timeout=ctx.tmux_control_timeout,
        )

    @mcp.tool(
        name="tmux_list",
        title="Tmux List Sessions",
        annotations=READ_ONLY_TOOL,
        description=(
            "List sessions on the configured tmux socket. By default each session includes a compact "
            "primary-pane summary; set include_panes=true to include every pane."
        ),
    )
    def tmux_list(
        include_panes: Annotated[
            bool,
            Field(description="Include all pane details for every session instead of only the primary pane."),
        ] = False,
    ) -> dict[str, object]:
        return client().list_sessions(include_panes=include_panes)

    @mcp.tool(
        name="tmux_start",
        title="Tmux Start Session",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Create one detached tmux session with one pane. Optionally start a command directly in the pane. "
            "The session survives individual MCP calls and can be attached manually with normal tmux commands."
        ),
    )
    def tmux_start(
        session: Annotated[
            str,
            Field(description="Session name using only letters, digits, underscore, or hyphen; maximum 64 characters."),
        ],
        cwd: Annotated[
            str | None,
            Field(description="Initial working directory. Defaults to the session cwd or workspace root."),
        ] = None,
        command: Annotated[
            str | None,
            Field(description="Optional shell command to run directly in the primary pane. Omit to start a normal shell."),
        ] = None,
        width: Annotated[
            int,
            Field(description="Detached terminal width in columns.", ge=40, le=400),
        ] = 180,
        height: Annotated[
            int,
            Field(description="Detached terminal height in rows.", ge=10, le=200),
        ] = 50,
        remain_on_exit: Annotated[
            bool,
            Field(description="Keep the pane after its command exits so output and exit status remain inspectable."),
        ] = True,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return client().start(
            session=session,
            cwd=resolved_cwd,
            command=command,
            width=width,
            height=height,
            remain_on_exit=remain_on_exit,
        )

    @mcp.tool(
        name="tmux_status",
        title="Tmux Session Status",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return structured status for one exact tmux session, including every pane, current command, cwd, PID, "
            "terminal size, and exit status for dead panes."
        ),
    )
    def tmux_status(
        session: Annotated[str, Field(description="Exact tmux session name to inspect.")],
    ) -> dict[str, object]:
        return client().status(session=session)

    @mcp.tool(
        name="tmux_capture",
        title="Tmux Capture Pane",
        annotations=READ_ONLY_TOOL,
        description=(
            "Capture recent visible terminal history from the primary pane of a tmux session. This is a terminal "
            "screen snapshot, not a lossless stdout/stderr log. Requests are capped to a bounded line and byte size."
        ),
    )
    def tmux_capture(
        session: Annotated[str, Field(description="Exact tmux session name to capture.")],
        lines: Annotated[
            int,
            Field(
                description=(
                    f"Number of history lines to request, capped at {MAX_CAPTURE_LINES}; the visible screen is also "
                    f"included and total returned output is capped at {MAX_CAPTURE_OUTPUT_LINES} lines."
                ),
                ge=1,
            ),
        ] = 100,
        join_wrapped: Annotated[
            bool,
            Field(description="Join soft-wrapped terminal lines using tmux capture-pane -J."),
        ] = True,
    ) -> dict[str, object]:
        return client().capture(
            session=session,
            lines=lines,
            join_wrapped=join_wrapped,
            max_tokens=ctx.tool_output_token_budget,
        )

    @mcp.tool(
        name="tmux_send",
        title="Tmux Send Input",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Send UTF-8 text and a small allowlisted set of keys to the primary pane. Text is loaded through tmux "
            "stdin buffers instead of being placed in shell arguments. Accepted-by-tmux does not guarantee the "
            "application consumed the input; use tmux_capture to verify."
        ),
    )
    def tmux_send(
        session: Annotated[str, Field(description="Exact tmux session name to receive input.")],
        text: Annotated[
            str | None,
            Field(description="Optional UTF-8 text to paste through a temporary tmux buffer."),
        ] = None,
        keys: Annotated[
            list[TmuxKey] | None,
            Field(description=f"Optional allowlisted special keys; maximum {MAX_KEYS_PER_CALL} entries."),
        ] = None,
        enter_count: Annotated[
            int,
            Field(
                description=f"Additional Enter keys to send after text/keys; between 0 and {MAX_ENTER_COUNT}.",
                ge=0,
                le=MAX_ENTER_COUNT,
            ),
        ] = 0,
    ) -> dict[str, object]:
        return client().send(
            session=session,
            text=text,
            keys=list(keys) if keys is not None else None,
            enter_count=enter_count,
        )

    @mcp.tool(
        name="tmux_kill",
        title="Tmux Kill Session",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Kill one exact tmux session. The operation is idempotent when the session is already absent. "
            "It never kills the whole tmux server or sessions selected by patterns."
        ),
    )
    def tmux_kill(
        session: Annotated[str, Field(description="Exact tmux session name to kill.")],
    ) -> dict[str, object]:
        return client().kill(session=session)

    return {
        "tmux_list": tmux_list,
        "tmux_start": tmux_start,
        "tmux_status": tmux_status,
        "tmux_capture": tmux_capture,
        "tmux_send": tmux_send,
        "tmux_kill": tmux_kill,
    }
