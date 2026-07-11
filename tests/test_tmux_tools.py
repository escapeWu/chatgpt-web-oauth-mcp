from __future__ import annotations

import asyncio


def test_tmux_tools_are_registered_with_expected_annotations_and_schemas() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> dict[str, dict[str, object]]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {
            tool.name: {
                "parameters": tool.parameters,
                "annotations": tool.annotations.model_dump(exclude_none=True),
            }
            for tool in tools
        }

    descriptors = asyncio.run(scenario())
    read_only = ["tmux_list", "tmux_status", "tmux_capture"]
    mutating = ["tmux_start", "tmux_send", "tmux_kill"]

    for name in read_only + mutating:
        assert name in descriptors

    for name in read_only:
        assert descriptors[name]["annotations"]["readOnlyHint"] is True
        assert descriptors[name]["annotations"]["openWorldHint"] is False

    for name in mutating:
        assert descriptors[name]["annotations"]["readOnlyHint"] is False
        assert descriptors[name]["annotations"]["openWorldHint"] is True

    send_properties = descriptors["tmux_send"]["parameters"]["properties"]
    assert send_properties["enter_count"]["minimum"] == 0
    assert send_properties["enter_count"]["maximum"] == 3
    assert set(send_properties["keys"]["anyOf"][0]["items"]["enum"]) >= {
        "Enter",
        "C-c",
        "C-d",
        "Escape",
    }

    start_properties = descriptors["tmux_start"]["parameters"]["properties"]
    assert start_properties["width"]["minimum"] == 40
    assert start_properties["width"]["maximum"] == 400
    assert start_properties["height"]["minimum"] == 10
    assert start_properties["height"]["maximum"] == 200


def test_server_info_reports_tmux_runtime() -> None:
    from chatgpt_web_oauth_mcp.server import server_info

    fn = server_info.fn if hasattr(server_info, "fn") else server_info
    payload = asyncio.run(fn())

    assert payload["success"] is True
    assert payload["tmux"]["socket_name"] == "default"
    assert isinstance(payload["tmux"]["available"], bool)
    for name in [
        "tmux_list",
        "tmux_start",
        "tmux_status",
        "tmux_capture",
        "tmux_send",
        "tmux_kill",
    ]:
        assert name in payload["tools"]
