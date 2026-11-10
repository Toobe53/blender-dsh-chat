#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Minimal ACP (Agent Client Protocol) v1 client for DeepSeek Harness.

No third-party dependencies, no bpy: it can be imported from Blender or tested
with a plain Python interpreter.

The client speaks newline-delimited JSON-RPC 2.0 to `dsh --profile <profile>`
over stdio.  Notifications are pushed as tuples into an event queue:

    ("status", text)                  connection progress
    ("ready", session_id, model)      session created, prompts accepted
    ("delta", text)                   assistant text chunk
    ("thought", text)                 reasoning chunk (display dim if you like)
    ("tool", title, status)           tool lifecycle
    ("usage", used, size)             context usage
    ("done", stop_reason)             prompt turn finished
    ("error", text)                   fatal problem / process exit

Usage:
    q = queue.Queue()
    client = ACPClient(q, cwd="/path", mcp_servers=[...])
    threading.Thread(target=client.start, daemon=True).start()
    ...
    client.prompt("你好")
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any

DSH_PROFILE = "blender"
DSH_BIN_CANDIDATES = ("/usr/local/bin/dsh", "/opt/homebrew/bin/dsh", "dsh")

def _dsh_binary() -> str:
    for candidate in DSH_BIN_CANDIDATES:
        if candidate == "dsh":
            found = shutil.which("dsh")
            if found:
                return found
        elif os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("dsh executable not found (install DeepSeek Harness or fix PATH)")


class ACPClient:
    """One `dsh --profile ...` subprocess and one ACP session."""

    def __init__(
        self,
        events: "queue.Queue[tuple]",
        *,
        cwd: str | None = None,
        profile: str = DSH_PROFILE,
        mcp_servers: list[dict[str, Any]] | None = None,
        model: str = '["deepseek-account","deepseek-flash"]',
        dsh_bin: str | None = None,
    ) -> None:
        self.events = events
        self.cwd = cwd or os.getcwd()
        self.profile = profile
        self.mcp_servers = mcp_servers or []
        self.model = model
        self.dsh_bin = dsh_bin

        self.proc: subprocess.Popen | None = None
        self.session_id: str | None = None
        self.configs: dict[str, dict[str, Any]] = {}
        self.ready = False
        self._next_id = 0
        self._lock = threading.Lock()
        self._waiters: dict[int, dict[str, Any]] = {}
        self._prompt_id: int | None = None
        self._closed = False
        self.tool_calls: dict[str, dict[str, Any]] = {}
        self.pending_permissions: dict[int, dict[str, Any]] = {}
        self.always_allow: set[str] = set()
        self._stderr_tail: list[str] = []

    # -- plumbing ---------------------------------------------------------

    def _emit(self, *event: Any) -> None:
        self.events.put(event)

    def _send(self, message: dict[str, Any]) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError("dsh process is not running")
        line = json.dumps(message, ensure_ascii=False)
        with self._lock:
            assert self.proc.stdin is not None
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        waiter: dict[str, Any] = {"event": threading.Event(), "result": None, "error": None}
        self._waiters[request_id] = waiter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not waiter["event"].wait(timeout):
            self._waiters.pop(request_id, None)
            raise TimeoutError(f"{method} timed out after {timeout:.0f}s")
        self._waiters.pop(request_id, None)
        if waiter["error"] is not None:
            raise RuntimeError(waiter["error"])
        return waiter["result"] or {}

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        try:
            binary = self.dsh_bin or _dsh_binary()
        except FileNotFoundError as exc:
            self._emit("error", str(exc))
            return

        self._emit("status", "启动 DSH…")
        env = os.environ.copy()
        # Blender launched from Finder has a minimal PATH: `dsh` needs node.
        env["PATH"] = os.pathsep.join(
            ["/usr/local/bin", "/opt/homebrew/bin", env.get("PATH", "")]
        )
        try:
            self.proc = subprocess.Popen(
                [binary, "--profile", self.profile],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=env,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            self._emit("error", f"无法启动 dsh：{exc}")
            return

        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._read_stdout, daemon=True).start()

        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                    },
                },
                timeout=30.0,
            )
            self._emit("status", "创建会话…")
            result = self._request(
                "session/new",
                {"cwd": self.cwd, "mcpServers": self.mcp_servers},
                timeout=90.0,
            )
            self.session_id = result.get("sessionId")
            if not self.session_id:
                raise RuntimeError("session/new returned no sessionId")
            self.configs.update({c.get("id"): c for c in (result.get("configOptions") or []) if c.get("id")})
            self._emit("config", self.configs)

            # Prefer the signed-in DeepSeek account route over an API key.
            try:
                switched = self._request(
                    "session/set_config_option",
                    {"sessionId": self.session_id, "configId": "model", "value": self.model},
                    timeout=20.0,
                )
                options = switched.get("configOptions") or []
                if options:
                    self.configs.update({c.get("id"): c for c in options if c.get("id")})
                    self._emit("config", self.configs)
            except Exception:
                pass

            self.ready = True
            self._emit("ready", self.session_id, self.model)
            threading.Thread(target=self._load_sessions, daemon=True).start()
            self._emit("status", "已连接")
        except Exception as exc:  # noqa: BLE001 - surface everything to the UI
            tail = "".join(self._stderr_tail[-3:]).strip()
            self._emit("error", f"{exc}" + (f"\n{tail}" if tail else ""))
            self.close()

    def set_config(self, config_id: str, value: str) -> None:
        """Change a session config option (model / reasoning effort)."""
        if not self.ready or not self.session_id:
            raise RuntimeError("session is not ready")
        result = self._request(
            "session/set_config_option",
            {"sessionId": self.session_id, "configId": config_id, "value": value},
            timeout=30.0,
        )
        options = result.get("configOptions") or []
        if options:
            self.configs.update({c.get("id"): c for c in options if c.get("id")})
            self._emit("config", self.configs)

    def list_sessions(self) -> list[dict[str, Any]]:
        if not self.ready:
            raise RuntimeError("session is not ready")
        result = self._request("session/list", {}, timeout=20.0)
        return result.get("sessions") or []

    def resume_session(self, session_id: str, cwd: str | None = None) -> str:
        target_cwd = cwd or self.cwd
        resumed = self._request(
            "session/resume",
            {"sessionId": session_id, "cwd": target_cwd, "mcpServers": self.mcp_servers},
            timeout=60.0,
        )
        self.cwd = target_cwd
        self.session_id = resumed.get("sessionId") or session_id
        options = resumed.get("configOptions") or []
        if options:
            self.configs.update({c.get("id"): c for c in options if c.get("id")})
            self._emit("config", self.configs)
        return self.session_id

    def resume_last(self) -> str | None:
        """Resume the most recent session in this cwd, if any."""
        sessions = self.list_sessions()
        match = next((s for s in sessions if s.get("cwd") == self.cwd), None)
        if match is None:
            return None
        return self.resume_session(match["sessionId"])

    def _load_sessions(self) -> None:
        try:
            self._emit("sessions", self.list_sessions()[:15])
        except Exception:
            pass

    def prompt(self, text: str) -> None:
        if not self.ready or not self.session_id:
            raise RuntimeError("session is not ready")
        self._next_id += 1
        self._prompt_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._prompt_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            }
        )

    def close(self) -> None:
        self._closed = True
        self.ready = False
        proc = self.proc
        self.proc = None
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass

    # -- readers ----------------------------------------------------------

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            stripped = line.rstrip()
            if stripped:
                self._stderr_tail.append(stripped + "\n")
                del self._stderr_tail[:-20]

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._dispatch(message)
            except Exception as exc:  # noqa: BLE001 - never kill the reader
                self._emit("error", f"处理消息失败：{exc}")
        if not self._closed:
            self._emit("status", "DSH 进程已退出")
            if self._stderr_tail:
                self._emit("error", "".join(self._stderr_tail[-4:])[:1500])

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "session/update":
            self._on_update(message.get("params") or {})
            return
        if method == "session/request_permission":
            self._on_permission(message)
            return
        if method is not None:
            # Unknown server request: answer with an error so the agent is not stuck.
            if "id" in message:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": f"unsupported: {method}"},
                    }
                )
            return

        request_id = message.get("id")
        if request_id in self._waiters:
            waiter = self._waiters[request_id]
            if "error" in message:
                waiter["error"] = message["error"].get("message", "request failed")
            else:
                waiter["result"] = message.get("result")
            waiter["event"].set()
            return
        if self._prompt_id is not None and request_id == self._prompt_id:
            result = message.get("result") or {}
            self._prompt_id = None
            self._emit("done", result.get("stopReason", "end_turn"))

    def _on_update(self, params: dict[str, Any]) -> None:
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = (update.get("content") or {}).get("text")
            if text:
                self._emit("delta", text)
        elif kind == "agent_thought_chunk":
            text = (update.get("content") or {}).get("text")
            if text:
                self._emit("thought", text)
        elif kind == "tool_call":
            call_id = update.get("toolCallId")
            if call_id:
                self.tool_calls[call_id] = {
                    "title": update.get("title") or "工具",
                    "kind": update.get("kind") or "",
                    "rawInput": update.get("rawInput"),
                }
            self._emit("tool", update.get("title") or update.get("toolCallId") or "tool", update.get("status", ""))
        elif kind == "tool_call_update":
            self._emit("tool", update.get("title") or "tool", update.get("status", ""))
        elif kind == "usage_update":
            self._emit("usage", update.get("used", 0), update.get("size", 0))

    def _on_permission(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        tool_call = params.get("toolCall") or {}
        info = self.tool_calls.get(tool_call.get("toolCallId"), {})
        title = info.get("title") or "工具"
        options = [
            {
                "optionId": option.get("optionId"),
                "name": option.get("name", option.get("optionId")),
                "kind": option.get("kind", ""),
            }
            for option in (params.get("options") or [])
        ]

        if title in self.always_allow:
            chosen = next((o for o in options if str(o.get("kind", "")).startswith("allow")), None)
            if chosen:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "result": {"outcome": {"outcome": "selected", "optionId": chosen["optionId"]}},
                    }
                )
                return

        request_id = message.get("id")
        self.pending_permissions[request_id] = {"title": title, "options": options}
        self._emit("permission", request_id, title, options)

    def answer_permission(self, request_id: int, option_id: str, always: bool = False) -> None:
        pending = self.pending_permissions.pop(request_id, None)
        if pending is None:
            return
        if always:
            self.always_allow.add(pending["title"])
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
            }
        )
