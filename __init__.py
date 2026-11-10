# SPDX-License-Identifier: MIT
"""DSH Chat — talk to a DeepSeek Harness agent inside Blender.

The add-on spawns `dsh --profile blender` (ACP over stdio), declares the
Blender MCP bridge as a tool server, and streams the conversation into the 3D
Viewport sidebar.  Tool calls made by the agent run in *this* Blender session,
so you can watch the viewport while you chat.

Install: copy this folder into Blender's `scripts/addons/`, enable "DSH Chat".
"""

bl_info = {
    "name": "DSH Chat",
    "author": "DeepSeek Harness",
    "version": (0, 6, 0),
    "blender": (5, 1, 0),
    "location": "3D Viewport > Sidebar > Item > DSH 对话",
    "description": "Chat with a DeepSeek Harness agent that can drive this Blender session",
    "category": "Development",
}

import json
import os
import queue
import shutil
import subprocess
import threading
import urllib.request

import blf
import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import Menu, Operator, Panel

from . import acp_client

EVENTS: "queue.Queue[tuple]" = queue.Queue()

STATE = {
    "client": None,
    "status": "未连接",
    "session": "",
    "usage": "",
    "busy": False,
    "ready": False,
    "items": [],
    "assistant_open": False,
    "pending": None,
    "configs": {},
    "permission": None,
    "sessions": [],
    "bundles": [],
    "balance": "",
    "balance_start": "",
    "balance_spent": "",
}

MAX_ITEMS = 300
CORE_BUNDLES = ("@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app")
TIMER_INTERVAL = 0.15



# ---------------------------------------------------------------------------
# state helpers


def _append(role: str, text: str) -> None:
    STATE["items"].append({"role": role, "text": text})
    if len(STATE["items"]) > MAX_ITEMS:
        del STATE["items"][: len(STATE["items"]) - MAX_ITEMS]


def _append_delta(text: str) -> None:
    if STATE["assistant_open"] and STATE["items"] and STATE["items"][-1]["role"] == "assistant":
        STATE["items"][-1]["text"] += text
    else:
        _append("assistant", text)
        STATE["assistant_open"] = True


def _log_text() -> str:
    prefix = {"user": "你", "assistant": "DSH", "tool": "工具", "system": "系统", "error": "错误"}
    return "\n\n".join(f"[{prefix.get(i['role'], i['role'])}] {i['text']}" for i in STATE["items"])


def _wrap_pixels(text: str, max_width: float) -> list[str]:
    """Wrap to the panel width using the actual UI font metrics (CJK-aware)."""
    result = []
    for paragraph in text.split("\n"):
        current = ""
        for char in paragraph:
            candidate = current + char
            if not current or blf.dimensions(0, candidate)[0] <= max_width:
                current = candidate
                continue
            cut = current.rfind(" ")
            if cut > len(current) * 0.55:
                result.append(current[:cut])
                current = current[cut + 1:] + char
            else:
                result.append(current)
                current = char
        result.append(current)
    return result or [""]


def _redraw() -> None:
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            area.tag_redraw()


def _flatten_options(config: dict | None) -> list[tuple[str, str, str]]:
    if not config:
        return []
    flat = []

    def add(option):
        flat.append(
            (
                str(option.get("value", "")),
                str(option.get("name", option.get("value", ""))),
                str(option.get("description", "")),
            )
        )

    for entry in config.get("options") or []:
        if "options" in entry:
            for option in entry.get("options") or []:
                add(option)
        else:
            add(entry)
    return flat


def _current_label(config_id: str) -> str:
    config = STATE["configs"].get(config_id)
    if not config:
        return "默认"
    current = config.get("currentValue")
    for value, label, _desc in _flatten_options(config):
        if value == current:
            return label
    return "默认"


def _dsh_home() -> str:
    return os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")


def _ensure_profile(profile: str) -> str:
    """Create the ACP profile on first run; return its package.json path."""
    folder = os.path.join(_dsh_home(), "profiles", profile)
    path = os.path.join(folder, "package.json")
    if os.path.exists(path):
        return path
    os.makedirs(folder, exist_ok=True)
    data = {
        "name": f"dsh-profile-{profile}",
        "private": True,
        "dependencies": {},
        "dsh": {
            "profile": {
                "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app"]
            }
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    return path


def _profile_path(profile: str | None = None) -> str:
    if not profile:
        prefs = _prefs(bpy.context)
        profile = (prefs.profile if prefs and prefs.profile else "blender")
    return os.path.join(_dsh_home(), "profiles", profile, "package.json")


def _read_bundles(profile: str | None = None) -> list[str]:
    try:
        with open(_profile_path(profile), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return list(data["dsh"]["profile"]["bundles"])
    except Exception:
        return []


def _write_bundles(bundles: list[str], profile: str | None = None) -> None:
    path = _profile_path(profile)
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("dsh", {}).setdefault("profile", {})["bundles"] = bundles
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    STATE["bundles"] = bundles


def _install_bundle_thread(path: str, profile: str) -> None:
    dsh = shutil.which("dsh") or "/usr/local/bin/dsh"
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(["/usr/local/bin", "/opt/homebrew/bin", env.get("PATH", "")])
    try:
        proc = subprocess.run(
            [dsh, "plugin", "--profile", profile, "add", f"file:{path}"],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout)[-500:])
        with open(os.path.join(path, "package.json"), "r", encoding="utf-8") as handle:
            name = json.load(handle).get("name")
        if not name:
            raise RuntimeError("package.json has no name")
        bundles = _read_bundles(profile)
        if name not in bundles:
            bundles.append(name)
            _write_bundles(bundles, profile)
        EVENTS.put(("plugins_changed", f"已安装 {name}，点『新会话』重连后生效"))
    except Exception as exc:  # noqa: BLE001
        EVENTS.put(("error", f"安装失败：{exc}"))


def _account_token() -> str | None:
    path = os.path.join(_dsh_home(), ".credentials.yaml")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    in_block = False
    block_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("deepseek-account-platform/default:"):
            in_block = True
            block_indent = indent
            continue
        if in_block:
            if indent <= block_indent:
                break
            if stripped.startswith("token:"):
                return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def _balance_thread(token: str) -> None:
    try:
        request = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"x-dsh-auth-token": token, "User-Agent": "dsh-blender-chat"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        infos = data.get("balance_infos") or []
        text = " · ".join(
            f"{item.get('currency', '?')} {item.get('total_balance', '?')}" for item in infos
        )
        EVENTS.put(("balance", text or "无余额信息"))
    except Exception as exc:  # noqa: BLE001
        EVENTS.put(("balance_error", str(exc)))


def _prefs(context):
    package = (__package__ or "blender_dsh_chat").split(".")[-1]
    addon = context.preferences.addons.get(package)
    return addon.preferences if addon else None


def _default_cwd(context=None) -> str:
    prefs = _prefs(context or bpy.context)
    if prefs and prefs.project_root and os.path.isdir(prefs.project_root):
        return prefs.project_root
    if bpy.data.filepath:
        return os.path.dirname(bpy.data.filepath)
    return os.path.expanduser("~")


def _start_client() -> None:
    if STATE["client"] is not None:
        return
    prefs = _prefs(bpy.context)
    profile = (prefs.profile if prefs and prefs.profile else "blender")
    try:
        _ensure_profile(profile)
    except OSError:
        pass
    python = (prefs.python_bin if prefs and prefs.python_bin else "") or shutil.which("python3") or "/usr/bin/python3"
    bridge = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blender_mcp_bridge.py")
    servers = [{"name": "blender", "command": python, "args": [bridge], "env": []}]
    client = acp_client.ACPClient(
        EVENTS,
        cwd=_default_cwd(bpy.context),
        profile=profile,
        mcp_servers=servers,
        dsh_bin=(prefs.dsh_bin if prefs and prefs.dsh_bin else None),
    )
    STATE["client"] = client
    STATE["status"] = "连接中…"
    threading.Thread(target=client.start, daemon=True).start()


def _apply_config(config_id: str, value: str) -> None:
    client = STATE.get("client")
    if client is None:
        return
    try:
        EVENTS.put(("status", "切换中…"))
        client.set_config(config_id, value)
        EVENTS.put(("status", "已连接"))
    except Exception as exc:  # noqa: BLE001
        EVENTS.put(("error", f"切换失败：{exc}"))


def _pump() -> float:
    changed = False
    while True:
        try:
            event = EVENTS.get_nowait()
        except queue.Empty:
            break
        changed = True
        kind = event[0]
        if kind == "status":
            STATE["status"] = event[1]
        elif kind == "ready":
            STATE["ready"] = True
            STATE["session"] = event[1]
            STATE["status"] = "已连接"
            STATE["bundles"] = _read_bundles()
            token = _account_token()
            if token:
                threading.Thread(target=_balance_thread, args=(token,), daemon=True).start()
            pending = STATE["pending"]
            STATE["pending"] = None
            if pending and STATE["client"]:
                STATE["busy"] = True
                try:
                    STATE["client"].prompt(pending)
                except Exception as exc:  # noqa: BLE001
                    STATE["busy"] = False
                    _append("error", str(exc))
        elif kind == "config":
            STATE["configs"] = dict(event[1] or {})
        elif kind == "delta":
            _append_delta(event[1])
        elif kind == "thought":
            pass
        elif kind == "tool":
            title, status = event[1], event[2] or ""
            text = f"🔧 {title}" + (f" · {status}" if status else "")
            if STATE["items"] and STATE["items"][-1]["role"] == "tool" and title in STATE["items"][-1]["text"]:
                STATE["items"][-1]["text"] = text
            else:
                _append("tool", text)
        elif kind == "usage":
            used, size = event[1], event[2]
            STATE["usage"] = f"{used / 1000:.1f}k / {size / 1000:.0f}k"
        elif kind == "plugins_changed":
            STATE["bundles"] = _read_bundles()
            _append("system", str(event[1]))
        elif kind == "balance":
            text = str(event[1])
            STATE["balance"] = text
            try:
                currency, amount = text.split(" ", 1)
                amount = float(amount.replace(",", ""))
                start_currency, start_amount = STATE["balance_start"].split(" ", 1)
                if start_currency != currency:
                    raise ValueError
                spent = float(start_amount) - amount
                STATE["balance_spent"] = f"{currency} {spent:.2f}" if spent >= 0 else ""
            except (ValueError, TypeError):
                STATE["balance_start"] = text
                STATE["balance_spent"] = ""
        elif kind == "balance_error":
            _append("system", f"余额查询失败：{event[1]}")
        elif kind == "sessions":
            STATE["sessions"] = list(event[1] or [])
        elif kind == "permission":
            STATE["permission"] = {"id": event[1], "title": event[2], "options": event[3]}
            STATE["status"] = "等待授权"
        elif kind == "done":
            STATE["busy"] = False
            STATE["assistant_open"] = False
            STATE["permission"] = None
            token = _account_token()
            if token:
                threading.Thread(target=_balance_thread, args=(token,), daemon=True).start()
        elif kind == "resumed":
            sid = event[1] or ""
            STATE["ready"] = bool(sid)
            STATE["busy"] = False
            STATE["assistant_open"] = False
            STATE["session"] = sid
            if sid:
                STATE["items"] = []
                _append("system", f"已续接会话 {sid[:8]}；之前的对话不在面板里，但 agent 记得。")
                STATE["status"] = "已续接"
            else:
                _append("system", "没有可续接的会话")
        elif kind == "error":
            STATE["busy"] = False
            STATE["ready"] = False
            STATE["status"] = "出错"
            STATE["permission"] = None
            client = STATE.get("client")
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            STATE["client"] = None
            _append("error", str(event[1]))

    if changed:
        _redraw()
    return TIMER_INTERVAL


# ---------------------------------------------------------------------------
# operators


class DSH_OT_send(Operator):
    bl_idname = "dsh.send"
    bl_label = "发送"
    bl_description = "把输入框里的消息发给 DSH"

    def execute(self, context):
        wm = context.window_manager
        text = (wm.dsh_input or "").strip()
        if not text:
            self.report({"WARNING"}, "消息为空")
            return {"CANCELLED"}
        wm.dsh_input = ""
        _append("user", text)
        STATE["assistant_open"] = False

        if STATE["client"] is None or not STATE["ready"]:
            STATE["pending"] = text
            _start_client()
        else:
            STATE["busy"] = True
            try:
                STATE["client"].prompt(text)
            except Exception as exc:  # noqa: BLE001
                STATE["busy"] = False
                _append("error", str(exc))
        _redraw()
        return {"FINISHED"}


class DSH_OT_stop(Operator):
    bl_idname = "dsh.stop"
    bl_label = "断开"
    bl_description = "结束 DSH 进程并断开连接"

    def execute(self, context):
        client = STATE["client"]
        if client:
            client.close()
        STATE.update(client=None, ready=False, busy=False, status="未连接", session="", usage="", configs={}, permission=None)
        _append("system", "已断开")
        _redraw()
        return {"FINISHED"}


class DSH_OT_new_session(Operator):
    bl_idname = "dsh.new_session"
    bl_label = "新会话"
    bl_description = "断开并新建一个干净的会话"

    def execute(self, context):
        client = STATE["client"]
        if client:
            client.close()
        STATE.update(
            client=None, ready=False, busy=False, status="未连接", session="", usage="",
            items=[], assistant_open=False, pending=None, configs={}, permission=None,
        )
        _start_client()
        _redraw()
        return {"FINISHED"}


def _resume_thread(client):
    try:
        EVENTS.put(("resumed", client.resume_last() or ""))
    except Exception as exc:  # noqa: BLE001
        EVENTS.put(("error", f"续接失败：{exc}"))


def _resume_id_thread(client, session_id, cwd):
    try:
        EVENTS.put(("resumed", client.resume_session(session_id, cwd or None)))
    except Exception as exc:  # noqa: BLE001
        EVENTS.put(("error", f"续接失败：{exc}"))


class DSH_OT_resume_id(Operator):
    bl_idname = "dsh.resume_id"
    bl_label = "续接会话"

    session_id: StringProperty()
    cwd: StringProperty()

    def execute(self, context):
        client = STATE["client"]
        if client is None or not STATE["ready"]:
            self.report({"WARNING"}, "先连接")
            return {"CANCELLED"}
        threading.Thread(
            target=_resume_id_thread, args=(client, self.session_id, self.cwd), daemon=True
        ).start()
        return {"FINISHED"}


class DSH_OT_resume(Operator):
    bl_idname = "dsh.resume"
    bl_label = "续接上次"
    bl_description = "恢复这个工作目录下最近一次会话（保留 agent 上下文）"

    def execute(self, context):
        client = STATE["client"]
        if client is None or not STATE["ready"]:
            self.report({"WARNING"}, "先连接")
            return {"CANCELLED"}
        threading.Thread(target=_resume_thread, args=(client,), daemon=True).start()
        return {"FINISHED"}


class DSH_OT_set_model(Operator):
    bl_idname = "dsh.set_model"
    bl_label = "选择模型"

    index: IntProperty()

    def execute(self, context):
        options = _flatten_options(STATE["configs"].get("model"))
        if not (0 <= self.index < len(options)):
            return {"CANCELLED"}
        threading.Thread(
            target=_apply_config, args=("model", options[self.index][0]), daemon=True
        ).start()
        return {"FINISHED"}


class DSH_OT_set_effort(Operator):
    bl_idname = "dsh.set_effort"
    bl_label = "选择思考深度"

    value: StringProperty()

    def execute(self, context):
        threading.Thread(
            target=_apply_config, args=("reasoning_effort", self.value), daemon=True
        ).start()
        return {"FINISHED"}


class DSH_OT_permission(Operator):
    bl_idname = "dsh.permission"
    bl_label = "处理授权"
    bl_description = "允许或拒绝 agent 的工具权限请求"

    request_id: IntProperty()
    option_id: StringProperty()
    always: BoolProperty(default=False)

    def execute(self, context):
        client = STATE.get("client")
        if client is not None:
            try:
                client.answer_permission(self.request_id, self.option_id, self.always)
            except Exception as exc:  # noqa: BLE001
                _append("error", f"授权失败：{exc}")
        STATE["permission"] = None
        STATE["status"] = "已连接" if STATE["ready"] else STATE["status"]
        _redraw()
        return {"FINISHED"}


class DSH_OT_refresh_balance(Operator):
    bl_idname = "dsh.refresh_balance"
    bl_label = "刷新余额"

    def execute(self, context):
        token = _account_token()
        if not token:
            self.report({"WARNING"}, "没找到 DeepSeek 账户 token")
            return {"CANCELLED"}
        threading.Thread(target=_balance_thread, args=(token,), daemon=True).start()
        return {"FINISHED"}


class DSH_OT_toggle_bundle(Operator):
    bl_idname = "dsh.toggle_bundle"
    bl_label = "开关插件"

    name: StringProperty()

    def execute(self, context):
        if self.name in CORE_BUNDLES:
            self.report({"WARNING"}, "核心 bundle 不能关闭")
            return {"CANCELLED"}
        bundles = _read_bundles()
        if self.name in bundles:
            bundles.remove(self.name)
            action = "已停用"
        else:
            bundles.append(self.name)
            action = "已启用"
        try:
            _write_bundles(bundles)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"写入 profile 失败：{exc}")
            return {"CANCELLED"}
        _append("system", f"{action} {self.name}，点『新会话』重连后生效")
        return {"FINISHED"}


class DSH_OT_install_bundle(Operator):
    bl_idname = "dsh.install_bundle"
    bl_label = "安装本地 Bundle"

    path: StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        self.layout.prop(self, "path")

    def execute(self, context):
        path = (self.path or "").strip()
        if not os.path.isdir(path):
            self.report({"ERROR"}, "选一个含 package.json 的目录")
            return {"CANCELLED"}
        prefs = _prefs(context)
        profile = prefs.profile if prefs and prefs.profile else "blender"
        threading.Thread(target=_install_bundle_thread, args=(path, profile), daemon=True).start()
        _append("system", f"正在安装 {os.path.basename(path)}…")
        return {"FINISHED"}


class DSH_OT_copy(Operator):
    bl_idname = "dsh.copy"
    bl_label = "复制记录"
    bl_description = "把完整对话复制到剪贴板，并写入一个 Text 数据块"

    def execute(self, context):
        text = _log_text()
        context.window_manager.clipboard = text
        block = bpy.data.texts.get("DSH 对话记录") or bpy.data.texts.new("DSH 对话记录")
        block.clear()
        block.write(text)
        self.report({"INFO"}, "已复制，完整记录在 Text『DSH 对话记录』")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# menus


class DSH_MT_model(Menu):
    bl_idname = "DSH_MT_model"
    bl_label = "模型"

    def draw(self, context):
        options = _flatten_options(STATE["configs"].get("model"))
        if not options:
            self.layout.label(text="连接后可选")
            return
        current = (STATE["configs"].get("model") or {}).get("currentValue")
        for index, (value, label, desc) in enumerate(options):
            row = self.layout.row()
            row.operator(
                "dsh.set_model",
                text=label,
                icon="CHECKMARK" if value == current else "BLANK1",
            ).index = index
            if desc:
                row.scale_y = 0.85


class DSH_MT_effort(Menu):
    bl_idname = "DSH_MT_effort"
    bl_label = "思考深度"

    def draw(self, context):
        options = _flatten_options(STATE["configs"].get("reasoning_effort"))
        if not options:
            self.layout.label(text="连接后可选")
            return
        current = (STATE["configs"].get("reasoning_effort") or {}).get("currentValue")
        for _value, label, desc in options:
            row = self.layout.row()
            row.operator(
                "dsh.set_effort",
                text=label,
                icon="CHECKMARK" if _value == current else "BLANK1",
            ).value = _value


# ---------------------------------------------------------------------------
# panel


class DSH_MT_plugins(Menu):
    bl_idname = "DSH_MT_plugins"
    bl_label = "插件"

    def draw(self, context):
        bundles = STATE.get("bundles") or _read_bundles()
        if not bundles:
            self.layout.label(text="未读取到 profile")
        for name in bundles:
            self.layout.operator(
                "dsh.toggle_bundle",
                text=name,
                icon="CHECKMARK",
            ).name = name
        self.layout.separator()
        self.layout.operator("dsh.install_bundle", text="安装本地 Bundle…", icon="IMPORT")


class DSH_MT_sessions(Menu):
    bl_idname = "DSH_MT_sessions"
    bl_label = "会话"

    def draw(self, context):
        sessions = STATE.get("sessions") or []
        if not sessions:
            self.layout.label(text="暂无历史会话")
            return
        current = STATE.get("session")
        for item in sessions:
            session_id = item.get("sessionId", "")
            if not session_id:
                continue
            label = f"{session_id[:8]}  {os.path.basename(item.get('cwd', '')) or '?'}"
            row = self.layout.row()
            op = row.operator(
                "dsh.resume_id",
                text=label,
                icon="CHECKMARK" if session_id == current else "BLANK1",
            )
            op.session_id = session_id
            op.cwd = item.get("cwd", "")


class DSHChatPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or "blender_dsh_chat"

    dsh_bin: StringProperty(name="dsh 可执行文件", description="留空自动查找", default="")
    profile: StringProperty(name="ACP profile", default="blender")
    python_bin: StringProperty(name="Python", description="留空自动查找 python3", default="")
    project_root: StringProperty(name="工作目录", subtype="DIR_PATH", default="")

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "dsh_bin")
        col.prop(self, "profile")
        col.prop(self, "python_bin")
        col.prop(self, "project_root")
        layout.separator()
        info = layout.column(align=True)
        info.label(text=f"dsh: {shutil.which('dsh') or '未找到'}")
        info.label(text=f"python3: {shutil.which('python3') or '未找到'}")
        profile_path = os.path.join(_dsh_home(), "profiles", self.profile, "package.json")
        info.label(text=f"profile: {'已存在' if os.path.exists(profile_path) else '首次连接时自动创建'}")


class DSH_PT_chat(Panel):
    bl_label = "DSH 对话"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Item"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager

        box = layout.box()
        col = box.column(align=True)
        col.label(text=STATE["status"], icon="LINKED" if STATE["ready"] else "UNLINKED")
        if STATE["session"]:
            col.label(text=f"会话 {STATE['session'][:8]}")
        if STATE["usage"]:
            col.label(text=f"上下文 {STATE['usage']}")
        if STATE["balance"]:
            col.operator("dsh.refresh_balance", text=f"余额 {STATE['balance']}", icon="FILE_REFRESH")
        if STATE["balance_spent"]:
            col.label(text=f"本次会话约 {STATE['balance_spent']}")
        if STATE["busy"]:
            col.label(text="思考中…", icon="SORTTIME")

        pending = STATE.get("permission")
        if pending:
            box = layout.box()
            box.label(text=f"授权请求 · {pending['title']}", icon="LOCKED")
            allow_id = next(
                (o["optionId"] for o in pending["options"] if str(o.get("kind", "")).startswith("allow")),
                None,
            )
            for option in pending["options"]:
                op = box.operator("dsh.permission", text=option["name"])
                op.request_id = pending["id"]
                op.option_id = option["optionId"]
                op.always = False
            if allow_id:
                op = box.operator("dsh.permission", text="本会话总是允许", icon="CHECKMARK")
                op.request_id = pending["id"]
                op.option_id = allow_id
                op.always = True

        if STATE["configs"]:
            layout.menu("DSH_MT_model", text=f"模型  {_current_label('model')}")
            layout.menu("DSH_MT_effort", text=f"思考  {_current_label('reasoning_effort')}")
            layout.menu(
                "DSH_MT_sessions",
                text=f"会话  {STATE['session'][:8] if STATE['session'] else '新会话'}",
            )
            layout.menu("DSH_MT_plugins", text=f"插件  {len(STATE['bundles'])} 个")

        max_width = max(80.0, (context.region.width - 28.0) * 0.82)
        lines = []
        for item in STATE["items"]:
            prefix = {"user": "你：", "assistant": "DSH：", "tool": "· ", "system": "· ", "error": "! "}.get(
                item["role"], ""
            )
            lines.extend(_wrap_pixels(prefix + item["text"], max_width))

        extra = 120 if pending else 0
        available = max(6, int((context.region.height - 290 - extra) / 17))
        for line in lines[-available:]:
            layout.label(text=line)

        layout.separator()
        layout.prop(wm, "dsh_input", text="")

        row = layout.row(align=True)
        row.operator("dsh.send", text="发送", icon="PLAY")
        row.operator("dsh.stop", text="断开", icon="PAUSE")
        row = layout.row(align=True)
        row.operator("dsh.new_session", text="新会话", icon="FILE_NEW")
        row.operator("dsh.resume", text="续接上次", icon="LOOP_BACK")
        row = layout.row(align=True)
        row.operator("dsh.copy", text="复制记录", icon="COPYDOWN")

        if STATE["client"] is None:
            layout.label(text="首次发送会自动连接", icon="INFO")


# ---------------------------------------------------------------------------
# registration


CLASSES = (
    DSHChatPreferences,
    DSH_OT_permission,
    DSH_OT_send,
    DSH_OT_stop,
    DSH_OT_new_session,
    DSH_OT_resume,
    DSH_OT_resume_id,
    DSH_OT_refresh_balance,
    DSH_OT_toggle_bundle,
    DSH_OT_install_bundle,
    DSH_MT_sessions,
    DSH_MT_plugins,
    DSH_OT_set_model,
    DSH_OT_set_effort,
    DSH_OT_copy,
    DSH_MT_model,
    DSH_MT_effort,
    DSH_PT_chat,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.dsh_input = StringProperty(name="消息", description="发给 DSH 的消息")
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, first_interval=0.2, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
    client = STATE.get("client")
    if client:
        try:
            client.close()
        except Exception:
            pass
    try:
        del bpy.types.WindowManager.dsh_input
    except AttributeError:
        pass
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
