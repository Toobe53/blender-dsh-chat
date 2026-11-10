#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bridge between an MCP client (stdio) and Blender's official MCP add-on.

The Blender add-on ("MCP" by Blender Lab, bundled with Blender 5.1+) exposes a
local TCP socket (default 127.0.0.1:9876) that executes Python inside the
running Blender session.  It does *not* speak MCP itself, so this script does
two jobs:

  * stdio MCP server  -- `python3 blender_mcp_bridge.py`  (what DSH launches)
  * one-shot CLI      -- `python3 blender_mcp_bridge.py --exec 'print(1)'`
                         `python3 blender_mcp_bridge.py --scene-info`

Socket protocol (NUL-terminated JSON, both directions):

    -> {"type": "execute", "code": "...", "strict_json": true}
    <- {"status": "ok", "result": {...}}   or   {"status": "error", ...}

Security note: this executes arbitrary Python in Blender, by design.  Keep the
socket bound to localhost.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9876
DEFAULT_TIMEOUT = 120.0

SCENE_INFO_CODE = """
import bpy

scene = bpy.context.scene
result = {
    "blender_version": bpy.app.version_string,
    "file": bpy.data.filepath,
    "scene": scene.name,
    "frame_current": scene.frame_current,
    "fps": scene.render.fps,
    "engine": scene.render.engine,
    "resolution": [scene.render.resolution_x, scene.render.resolution_y],
    "objects": [
        {
            "name": obj.name,
            "type": obj.type,
            "location": [round(v, 3) for v in obj.location],
            "visible": not obj.hide_viewport,
        }
        for obj in scene.objects
    ],
    "collections": [col.name for col in bpy.data.collections],
    "materials": [mat.name for mat in bpy.data.materials],
    "actions": [act.name for act in bpy.data.actions],
}
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "execute_blender_code",
        "description": (
            "Execute Python code inside the running Blender session and return "
            "its result. Assign the value to return to the variable `result` "
            "(it must be JSON-serialisable; use repr() for Blender types). "
            "`bpy` is already imported in the target namespace and stdout/stderr "
            "are captured. Use this to inspect or modify the scene, animate "
            "objects, render frames, export files, etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source to run in Blender.",
                }
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_scene_info",
        "description": (
            "Summarise the current Blender scene: file path, scene name, frame, "
            "render settings, objects, collections, materials and actions."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

SERVER_INFO = {"name": "blender-mcp-bridge", "version": "1.0.0"}
INSTRUCTIONS = (
    "Controls a running Blender session through the official Blender Lab MCP "
    "add-on. Prefer get_scene_info before editing; verify destructive edits; "
    "assign JSON-serialisable values to `result`."
)


# ---------------------------------------------------------------------------
# Blender socket client


class BlenderError(RuntimeError):
    """Raised when the Blender add-on cannot be reached or returns an error."""


def execute_blender_code(
    code: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run *code* in Blender and return the add-on's decoded response."""
    request = {"type": "execute", "code": code, "strict_json": True}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(request) + "\0").encode("utf-8"))
            buf = bytearray()
            while b"\0" not in buf:
                chunk = sock.recv(1 << 16)
                if not chunk:
                    break
                buf.extend(chunk)
    except OSError as exc:
        raise BlenderError(
            f"cannot reach the Blender MCP add-on at {host}:{port} ({exc}). "
            "Open Blender 5.1+ and enable the 'MCP' add-on; it starts the "
            "socket server automatically (check its preferences for the port)."
        ) from exc

    payload = bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace")
    if not payload:
        raise BlenderError("Blender closed the connection without a response")
    try:
        response = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BlenderError(f"invalid JSON from Blender: {payload[:400]!r}") from exc
    if not isinstance(response, dict):
        raise BlenderError(f"unexpected response from Blender: {response!r}")
    return response


def format_response(response: dict[str, Any]) -> tuple[str, bool]:
    """Turn an add-on response into (text, is_error) for an MCP tool result."""
    status = response.get("status")
    if status == "ok":
        return json.dumps(response.get("result"), ensure_ascii=False, indent=2), False
    if status == "error":
        text = response.get("message") or "Blender reported an error"
        extra = []
        for key in ("stdout", "stderr"):
            if response.get(key):
                extra.append(f"{key}:\n{response[key]}")
        if extra:
            text += "\n\n" + "\n\n".join(extra)
        return text, True
    return json.dumps(response, ensure_ascii=False, indent=2), False


# ---------------------------------------------------------------------------
# stdio MCP server


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id: Any, result: dict[str, Any]) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _call_tool(name: str, arguments: dict[str, Any], options: argparse.Namespace) -> dict[str, Any]:
    if name == "get_scene_info":
        code = SCENE_INFO_CODE
    elif name == "execute_blender_code":
        code = arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            return {
                "content": [{"type": "text", "text": "`code` must be a non-empty string"}],
                "isError": True,
            }
    else:
        return {
            "content": [{"type": "text", "text": f"unknown tool: {name}"}],
            "isError": True,
        }

    try:
        response = execute_blender_code(
            code, host=options.host, port=options.port, timeout=options.timeout
        )
    except BlenderError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}

    text, is_error = format_response(response)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def serve(options: argparse.Namespace) -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        # Responses to server-initiated requests and notifications.
        if method is None or request_id is None:
            continue

        try:
            if method == "initialize":
                _result(
                    request_id,
                    {
                        "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                        "instructions": INSTRUCTIONS,
                    },
                )
            elif method == "ping":
                _result(request_id, {})
            elif method == "tools/list":
                _result(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                _result(
                    request_id,
                    _call_tool(
                        params.get("name", ""),
                        params.get("arguments") or {},
                        options,
                    ),
                )
            elif method in ("resources/list", "resources/templates/list"):
                _result(request_id, {"resources" if method == "resources/list" else "resourceTemplates": []})
            elif method == "prompts/list":
                _result(request_id, {"prompts": []})
            else:
                _error(request_id, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            _error(request_id, -32603, f"internal error: {exc}")
    return 0


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="MCP stdio bridge for Blender's official MCP add-on. "
        "With no mode flag it serves MCP on stdin/stdout.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Blender add-on host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Blender add-on port")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="socket timeout (seconds)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--exec", dest="code", help="run this Python code in Blender and print the result")
    group.add_argument("--exec-file", help="run Python from a file in Blender and print the result")
    group.add_argument("--scene-info", action="store_true", help="print a summary of the current scene")
    args = parser.parse_args(argv)

    if args.scene_info:
        args.code = SCENE_INFO_CODE
    elif args.exec_file:
        try:
            with open(args.exec_file, "r", encoding="utf-8") as handle:
                args.code = handle.read()
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.code is not None:
        try:
            response = execute_blender_code(
                args.code, host=args.host, port=args.port, timeout=args.timeout
            )
        except BlenderError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        text, is_error = format_response(response)
        stream = sys.stderr if is_error else sys.stdout
        print(text, file=stream)
        return 1 if is_error else 0

    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
