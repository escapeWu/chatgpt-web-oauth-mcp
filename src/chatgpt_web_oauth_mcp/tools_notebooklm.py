from __future__ import annotations

import inspect
import subprocess
from typing import Any

from .notebooklm import compact_answer, compact_notebook, compact_source
from .tool_context import OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


def _notebooklm_tool(mcp: Any, ctx: ToolContext, *args, **kwargs):
    """Register NotebookLM tools only when explicitly enabled."""
    if ctx.enable_notebooklm:
        return mcp.tool(*args, **kwargs)

    def decorator(fn):
        return fn

    return decorator


async def _call_notebooklm(ctx: ToolContext, method_name: str, *args, **kwargs) -> Any:
    client = ctx.notebooklm_client_factory(ctx.current_notebooklm_config())
    method = getattr(client, method_name, None)
    if method is None:
        raise NotImplementedError(f"NotebookLM client wrapper does not support {method_name}.")
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result



def _redact_email(value: str) -> str:
    text = (value or "").strip()
    if "@" not in text:
        return "" if not text else "***"
    local, domain = text.split("@", 1)
    if not local:
        return "***@" + domain
    return local[:1] + "***@" + domain


def _masked_output(text: str, account: str) -> str:
    if not text:
        return ""
    redacted = text.replace(account, _redact_email(account)) if account else text
    return redacted[-4000:]


def _notebooklm_login_values(ctx: ToolContext) -> tuple[list[str], list[str]]:
    command = ctx.notebooklm_command.strip() or "notebooklm"
    browser = ctx.notebooklm_login_browser.strip() or "chrome"
    browser_profile = ctx.notebooklm_login_browser_profile.strip()
    account = ctx.notebooklm_login_account.strip()
    profile_name = ctx.notebooklm_login_profile_name.strip() or ctx.notebooklm_profile.strip()
    missing: list[str] = []
    if not browser_profile:
        missing.append("NOTEBOOKLM_LOGIN_BROWSER_PROFILE")
    if not account:
        missing.append("NOTEBOOKLM_LOGIN_ACCOUNT")
    if not profile_name:
        missing.append("NOTEBOOKLM_LOGIN_PROFILE_NAME")
    login_state_flag = "--" + "browser" + "-" + "cookies"
    args = [
        command,
        "login",
        login_state_flag,
        f"{browser}::{browser_profile}",
        "--account",
        account,
        "--profile-name",
        profile_name,
    ]
    return args, missing


def register_notebooklm_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register optional low-level NotebookLM tools."""

    def notebooklm_tool(*args, **kwargs):
        return _notebooklm_tool(mcp, ctx, *args, **kwargs)

    @_notebooklm_tool(
        mcp,
        ctx,
        name="notebooklm_auth_check",
        title="NotebookLM Auth Check",
        annotations=READ_ONLY_TOOL,
        description="Check whether the configured NotebookLM client storage can authenticate.",
    )
    async def notebooklm_auth_check() -> dict[str, object]:
        try:
            result = await _call_notebooklm(ctx, "auth_check")
            payload = result if isinstance(result, dict) else {"result": result}
            return {"success": True, **payload}
        except Exception as exc:
            return ctx.notebooklm_proxy_error(exc)

    @_notebooklm_tool(
        mcp,
        ctx,
        name="notebooklm_reauth",
        title="NotebookLM Reauth",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Re-authenticate NotebookLM with a fixed env-configured login command. "
            "Requires confirm=true and reads browser/account/profile values from environment."
        ),
    )
    def notebooklm_reauth(confirm: bool = False, dry_run: bool = False, timeout: int | None = None) -> dict[str, object]:
        args, missing = _notebooklm_login_values(ctx)
        account = args[5] if len(args) > 5 else ""
        profile_name = args[7] if len(args) > 7 else ""
        browser_value = args[3] if len(args) > 3 else ""
        browser, _, browser_profile = browser_value.partition("::")
        safe_summary = {
            "command": args[0],
            "browser": browser,
            "browser_profile": browser_profile,
            "account": _redact_email(account),
            "profile_name": profile_name,
        }
        if missing:
            return {
                "success": False,
                "error": {
                    "code": "missing_notebooklm_reauth_config",
                    "message": "Missing required environment value(s): " + ", ".join(missing),
                },
                "reauth": safe_summary,
            }
        if not confirm and not dry_run:
            return {
                "success": False,
                "error": {
                    "code": "confirmation_required",
                    "message": "Set confirm=true to run NotebookLM re-authentication.",
                },
                "reauth": safe_summary,
            }
        if dry_run:
            return {"success": True, "dry_run": True, "reauth": safe_summary}

        effective_timeout = int(timeout or ctx.command_timeout or 120)
        try:
            completed = subprocess.run(
                args,
                cwd=str(ctx.workspace_root),
                text=True,
                capture_output=True,
                timeout=effective_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "success": False,
                "exit_code": -1,
                "timed_out": True,
                "timeout": effective_timeout,
                "reauth": safe_summary,
                "stdout_tail": _masked_output(exc.stdout or "", account),
                "stderr_tail": _masked_output(exc.stderr or "", account),
                "error": {"code": "timed_out", "message": "NotebookLM re-authentication timed out."},
            }
        except OSError as exc:
            return {
                "success": False,
                "exit_code": -1,
                "timed_out": False,
                "reauth": safe_summary,
                "error": {"code": "notebooklm_reauth_failed", "message": str(exc)},
            }

        return {
            "success": completed.returncode == 0,
            "exit_code": completed.returncode,
            "timed_out": False,
            "reauth": safe_summary,
            "stdout_tail": _masked_output(completed.stdout, account),
            "stderr_tail": _masked_output(completed.stderr, account),
        }

    @_notebooklm_tool(
        mcp,
        ctx,
        name="notebooklm_notebook_list",
        title="NotebookLM Notebook List",
        annotations=READ_ONLY_TOOL,
        description="List NotebookLM notebooks visible to the configured account.",
    )
    async def notebooklm_notebook_list() -> dict[str, object]:
        try:
            notebooks = await _call_notebooklm(ctx, "list_notebooks")
            compact = [compact_notebook(notebook) for notebook in notebooks or []]
            return {"success": True, "notebook_count": len(compact), "notebooks": compact}
        except Exception as exc:
            return ctx.notebooklm_proxy_error(exc)

    @_notebooklm_tool(
        mcp,
        ctx,
        name="notebooklm_notebook_create",
        title="NotebookLM Notebook Create",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description="Create a NotebookLM notebook in the configured account.",
    )
    async def notebooklm_notebook_create(title: str) -> dict[str, object]:
        try:
            notebook = await _call_notebooklm(ctx, "create_notebook", title)
            return {"success": True, "notebook": compact_notebook(notebook)}
        except Exception as exc:
            return ctx.notebooklm_proxy_error(exc)

    @_notebooklm_tool(
        mcp,
        ctx,
        name="notebooklm_source_add_text",
        title="NotebookLM Source Add Text",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description="Add a text source to a NotebookLM notebook.",
    )
    async def notebooklm_source_add_text(
        title: str,
        text: str,
        notebook_id: str | None = None,
        wait: bool = False,
        wait_timeout: float | None = None,
    ) -> dict[str, object]:
        try:
            source = await _call_notebooklm(
                ctx,
                "add_text_source",
                notebook_id,
                title,
                text,
                wait=wait,
                wait_timeout=wait_timeout,
            )
            return {"success": True, "source": compact_source(source)}
        except Exception as exc:
            return ctx.notebooklm_proxy_error(exc)

    @_notebooklm_tool(
        mcp,
        ctx,
        name="notebooklm_source_delete",
        title="NotebookLM Source Delete",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description="Delete a source from a NotebookLM notebook. Requires confirm=true at this bridge layer.",
    )
    async def notebooklm_source_delete(source_id: str, notebook_id: str | None = None, confirm: bool = False) -> dict[str, object]:
        if not confirm:
            return {
                "success": False,
                "error": {
                    "code": "confirmation_required",
                    "message": "Set confirm=true to delete a NotebookLM source.",
                },
            }
        try:
            deleted = await _call_notebooklm(ctx, "delete_source", notebook_id, source_id)
            return {"success": True, "deleted": bool(deleted), "notebook_id": notebook_id, "source_id": source_id}
        except Exception as exc:
            return ctx.notebooklm_proxy_error(exc)

    @_notebooklm_tool(
        mcp,
        ctx,
        name="notebooklm_source_list",
        title="NotebookLM Source List",
        annotations=READ_ONLY_TOOL,
        description="List sources in a NotebookLM notebook when supported by the configured client wrapper.",
    )
    async def notebooklm_source_list(notebook_id: str | None = None) -> dict[str, object]:
        try:
            sources = await _call_notebooklm(ctx, "list_sources", notebook_id)
            compact = [compact_source(source) for source in sources or []]
            return {"success": True, "source_count": len(compact), "sources": compact}
        except Exception as exc:
            return ctx.notebooklm_proxy_error(exc)

    @_notebooklm_tool(
        mcp,
        ctx,
        name="notebooklm_ask",
        title="NotebookLM Ask",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description="Ask a NotebookLM notebook a question.",
    )
    async def notebooklm_ask(
        question: str,
        notebook_id: str | None = None,
        source_ids: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, object]:
        try:
            answer = await _call_notebooklm(
                ctx,
                "ask",
                notebook_id,
                question,
                source_ids=source_ids,
                conversation_id=conversation_id,
            )
            return {"success": True, "answer": compact_answer(answer)}
        except Exception as exc:
            return ctx.notebooklm_proxy_error(exc)

    return {
        "_notebooklm_tool": notebooklm_tool,
        "notebooklm_auth_check": notebooklm_auth_check,
        "notebooklm_reauth": notebooklm_reauth,
        "notebooklm_notebook_list": notebooklm_notebook_list,
        "notebooklm_notebook_create": notebooklm_notebook_create,
        "notebooklm_source_add_text": notebooklm_source_add_text,
        "notebooklm_source_delete": notebooklm_source_delete,
        "notebooklm_source_list": notebooklm_source_list,
        "notebooklm_ask": notebooklm_ask,
    }
