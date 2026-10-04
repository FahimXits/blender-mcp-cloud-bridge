# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Blender add-on that provides an MCP socket bridge-server with Cloud Tunnel support.
"""

__all__ = (
    "register",
    "unregister",
)

import secrets
import bpy  # pylint: disable=import-error
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)  # pylint: disable=import-error

from . import mcp_to_blender_server
from . import cloud_bridge

_PORT_MIN = 1024
_PORT_MAX = 65535

_AUTOSTART_DELAY = 1.0
_cli_commands: list[object] = []
_state_offline_error_message = "Online access must be enabled in the system preferences"


class _State:
    """Module-level runtime state that is not persisted across sessions."""
    autostart_error: str = ""

    @classmethod
    def startup_info_set(cls, error: str) -> None:
        cls.autostart_error = error

    @classmethod
    def startup_info_set_from_exception(cls, ex: Exception) -> None:
        cls.autostart_error = str(ex)

    @classmethod
    def startup_info_clear(cls) -> None:
        cls.autostart_error = ""

    @classmethod
    def startup_online_ok_or_error(cls) -> bool:
        if bpy.app.online_access:
            return True
        cls.startup_info_set(_state_offline_error_message)
        if bpy.app.background:
            print("Error: {:s}".format(_state_offline_error_message))
            print("  Use --online-mode to enable online access from the command line")
        return False


class _BlenderMCPPreferences(bpy.types.AddonPreferences):  # type: ignore[misc]
    bl_idname = __package__

    host: StringProperty(  # type: ignore[valid-type]
        name="Host",
        default="127.0.0.1",
    )
    port: IntProperty(  # type: ignore[valid-type]
        name="Port",
        default=mcp_to_blender_server.DEFAULT_PORT,
        min=_PORT_MIN,
        max=_PORT_MAX,
    )
    use_autostart: BoolProperty(  # type: ignore[valid-type]
        name="Auto Start Local Server",
        description="Automatically start the local MCP socket server when Blender starts",
        default=True,
    )
    autostart_delay: FloatProperty(  # type: ignore[valid-type]
        name="Auto Start Delay",
        description="Seconds to wait after Blender starts before auto-starting the server",
        default=_AUTOSTART_DELAY,
        min=0.0,
        max=30.0,
        step=10,
        precision=1,
        subtype="TIME_ABSOLUTE",
    )

    def _update_use_log(self, _context: bpy.types.Context) -> None:
        mcp_to_blender_server.use_log = self.use_log

    use_log: BoolProperty(  # type: ignore[valid-type]
        name="Log",
        description="Print every tool request and response status to the terminal",
        default=False,
        update=_update_use_log,
    )

    def _update_timer_interval_active(self, _context: bpy.types.Context) -> None:
        mcp_to_blender_server.timer_internal_vars_calc(active=self.timer_interval_active)

    timer_interval_active: FloatProperty(  # type: ignore[valid-type]
        name="Timer Interval",
        description="Seconds between queue polling ticks in interactive mode",
        default=0.25,
        min=0.05,
        max=5.0,
        step=1,
        precision=2,
        subtype='TIME_ABSOLUTE',
        update=_update_timer_interval_active,
    )

    def _update_timer_interval_idle(self, _context: bpy.types.Context) -> None:
        mcp_to_blender_server.timer_internal_vars_calc(idle=self.timer_interval_idle)

    timer_interval_idle: FloatProperty(  # type: ignore[valid-type]
        name="Timer Interval Idle",
        description="Seconds between queue polling ticks while idle",
        default=1.0,
        min=0.1,
        max=10.0,
        step=10,
        precision=2,
        subtype='TIME_ABSOLUTE',
        update=_update_timer_interval_idle,
    )

    def _update_timer_interval_idle_delay(self, _context: bpy.types.Context) -> None:
        mcp_to_blender_server.timer_internal_vars_calc(idle_delay=self.timer_interval_idle_delay)

    timer_interval_idle_delay: FloatProperty(  # type: ignore[valid-type]
        name="Idle Delay",
        description="Seconds of inactivity before switching to the idle polling interval",
        default=5.0,
        min=1.0,
        max=60.0,
        step=100,
        precision=1,
        subtype='TIME_ABSOLUTE',
        update=_update_timer_interval_idle_delay,
    )

    # Cloud Bridge Settings
    tunnel_provider: EnumProperty(  # type: ignore[valid-type]
        name="Tunnel Provider",
        description="Public tunneling service to expose Blender to your cloud AI assistant",
        items=[
            ("ngrok", "ngrok (Recommended for Cloud AI)", "Uses ngrok to prevent cloud datacenter IP blocks"),
            ("cloudflare", "Cloudflare Quick Tunnel", "Uses trycloudflare.com"),
        ],
        default="ngrok",
    )

    ngrok_authtoken: StringProperty(  # type: ignore[valid-type]
        name="ngrok Token",
        description="Your free ngrok authtoken from dashboard.ngrok.com",
        default="",
        subtype="PASSWORD",
    )

    cloud_api_key: StringProperty(  # type: ignore[valid-type]
        name="Agent API Key",
        description="Secret key required from your cloud AI assistant to prevent unauthorized access",
        default="",
    )

    cloud_http_port: IntProperty(  # type: ignore[valid-type]
        name="Bridge HTTP Port",
        description="Local HTTP port used for the Cloud Bridge (tunnel targets this port)",
        default=8765,
        min=_PORT_MIN,
        max=_PORT_MAX,
    )

    def draw(self, context: bpy.types.Context) -> None:
        del context
        layout = self.layout

        # --- Section 1: Local MCP Server ---
        box_local = layout.box()
        box_local.label(text="Local MCP Server (Core)", icon="PREFERENCES")
        row = box_local.row(align=True)
        row.prop(self, "host")
        row.prop(self, "port")
        row2 = box_local.row(align=True)
        row2.prop(self, "use_autostart")
        row2.prop(self, "autostart_delay")
        box_local.prop(self, "use_log")

        row_status = box_local.row()
        if mcp_to_blender_server.is_running():
            row_status.operator("blmcp.server_stop", icon="CANCEL", text="Stop Local Server")
            row_status.label(text=f"Running ({self.host}:{self.port})", icon="CHECKMARK")
        else:
            row_status.operator("blmcp.server_start", icon="PLAY", text="Start Local Server")
            row_status.label(text="Local Server is Stopped", icon="X")

        if _State.autostart_error:
            box_local.label(text=_State.autostart_error, icon="ERROR")

        # --- Section 2: Public Cloud AI Assistant Bridge ---
        box_cloud = layout.box()
        box_cloud.label(text="Cloud AI Assistant Bridge (Public HTTPS URL)", icon="WORLD")
        box_cloud.prop(self, "tunnel_provider")

        if self.tunnel_provider == "ngrok":
            row_token = box_cloud.row(align=True)
            row_token.prop(self, "ngrok_authtoken")
            row_token.operator("blmcp.open_ngrok_signup", text="Get Free Token", icon="URL")

            if not cloud_bridge.find_executable("ngrok"):
                col_warn = box_cloud.column(align=True)
                col_warn.alert = True
                row_w = col_warn.row(align=True)
                row_w.label(text="ngrok executable not found", icon="ERROR")
                op = row_w.operator("blmcp.download_binary", text="Download ngrok (1-Click)", icon="IMPORT")
                op.binary_name = "ngrok"

        elif self.tunnel_provider == "cloudflare":
            if not cloud_bridge.find_executable("cloudflared"):
                col_warn = box_cloud.column(align=True)
                col_warn.alert = True
                row_w = col_warn.row(align=True)
                row_w.label(text="cloudflared executable not found", icon="ERROR")
                op = row_w.operator("blmcp.download_binary", text="Download Cloudflared (1-Click)", icon="IMPORT")
                op.binary_name = "cloudflared"

        # Security API Key box
        box_key = box_cloud.box()
        box_key.label(text="Agent Security API Key (Required for incoming requests)", icon="LOCKED")
        row_key = box_key.row(align=True)
        row_key.prop(self, "cloud_api_key")
        row_key.operator("blmcp.cloud_generate_key", text="Generate New", icon="FILE_REFRESH")
        row_key.operator("blmcp.cloud_copy_key", text="Copy Key", icon="COPYDOWN")

        row_cfg = box_cloud.row(align=True)
        row_cfg.prop(self, "cloud_http_port")

        bridge_running = cloud_bridge.is_running()
        pub_url = cloud_bridge.get_public_url()

        row_bridge_ctl = box_cloud.row()
        if bridge_running:
            row_bridge_ctl.operator("blmcp.cloud_stop", icon="CANCEL", text="Stop Cloud Bridge")
            status_text = f"Active: {pub_url}" if pub_url else "Starting Tunnel..."
            row_bridge_ctl.label(text=status_text, icon="CHECKMARK")
        else:
            row_bridge_ctl.operator("blmcp.cloud_start", icon="PLAY", text="Start Cloud Bridge")
            row_bridge_ctl.label(text="Cloud Bridge Stopped", icon="X")

        if bridge_running and pub_url:
            sub = box_cloud.box()
            row_url = sub.row(align=True)
            row_url.label(text=f"Public URL: {pub_url}", icon="URL")
            row_url.operator("blmcp.cloud_copy_url", text="Copy URL", icon="COPYDOWN")
            row_url.operator("blmcp.cloud_copy_agent_prompt", text="Copy Agent Config", icon="TEXT")

            sub.label(text="Endpoints for your Cloud Assistant:", icon="INFO")
            sub.label(text=f" • MCP SSE     : {pub_url}/sse")
            sub.label(text=f" • MCP Direct  : {pub_url}/mcp")
            sub.label(text=f" • REST POST   : {pub_url}/execute")
            sub.label(text=f" • OpenAPI Doc : {pub_url}/openapi.json")


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class _BLMCP_OT_server_start(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.server_start"
    bl_label = "Start MCP Bridge Server"
    bl_description = "Start the local MCP socket bridge server"

    def execute(self, context: bpy.types.Context) -> set[str]:
        from . import execute_interactive

        if bpy.app.background:
            self.report({"ERROR"}, "Use `--command blender_mcp` in background mode")
            return {"CANCELLED"}
        if not _State.startup_online_ok_or_error():
            self.report({"ERROR"}, _state_offline_error_message)
            return {"CANCELLED"}
        _State.startup_info_clear()
        prefs = context.preferences.addons[__package__].preferences
        mcp_to_blender_server.timer_internal_vars_calc(
            active=prefs.timer_interval_active,
            idle=prefs.timer_interval_idle,
            idle_delay=prefs.timer_interval_idle_delay,
        )
        mcp_to_blender_server.use_log = prefs.use_log
        try:
            mcp_to_blender_server.start(prefs.host, prefs.port)
        except Exception as ex:
            _State.startup_info_set_from_exception(ex)
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        bpy.app.timers.register(
            execute_interactive.run,
            first_interval=mcp_to_blender_server.TIMER_INTERVAL_ACTIVE,
            persistent=True)
        self.report({"INFO"}, f"MCP server started on {prefs.host}:{prefs.port}")
        return {"FINISHED"}


class _BLMCP_OT_server_stop(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.server_stop"
    bl_label = "Stop MCP Server"
    bl_description = "Stop the local MCP Bridge Server"

    def execute(self, context: bpy.types.Context) -> set[str]:
        del context
        from . import execute_interactive
        _State.startup_info_clear()
        mcp_to_blender_server.stop()
        if bpy.app.timers.is_registered(execute_interactive.run):
            bpy.app.timers.unregister(execute_interactive.run)
        self.report({"INFO"}, "MCP bridge server stopped")
        return {"FINISHED"}


class _BLMCP_OT_cloud_start(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.cloud_start"
    bl_label = "Start Cloud Bridge"
    bl_description = "Start the embedded Cloud Bridge and Tunnel"

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not mcp_to_blender_server.is_running():
            bpy.ops.blmcp.server_start()

        prefs = context.preferences.addons[__package__].preferences
        if not prefs.cloud_api_key:
            prefs.cloud_api_key = secrets.token_urlsafe(24)

        success, msg = cloud_bridge.start(
            blender_host=prefs.host,
            blender_port=prefs.port,
            http_port=prefs.cloud_http_port,
            api_key=prefs.cloud_api_key,
            ngrok_authtoken=prefs.ngrok_authtoken.strip(),
            tunnel_provider=prefs.tunnel_provider,
        )

        if not success:
            self.report({"ERROR"}, f"Cloud bridge failed to start: {msg}")
            return {"CANCELLED"}

        pub_url = cloud_bridge.get_public_url()
        if pub_url:
            self.report({"INFO"}, f"Cloud Bridge active at {pub_url}")
        else:
            self.report({"INFO"}, "Cloud Bridge starting tunnel in background...")
        return {"FINISHED"}


class _BLMCP_OT_cloud_stop(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.cloud_stop"
    bl_label = "Stop Cloud Bridge"
    bl_description = "Stop the Cloud Bridge and Tunnel"

    def execute(self, context: bpy.types.Context) -> set[str]:
        del context
        cloud_bridge.stop()
        self.report({"INFO"}, "Cloud bridge stopped")
        return {"FINISHED"}


class _BLMCP_OT_cloud_generate_key(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.cloud_generate_key"
    bl_label = "Generate New API Key"
    bl_description = "Generate a new cryptographically secure API Key for your cloud agent"

    def execute(self, context: bpy.types.Context) -> set[str]:
        prefs = context.preferences.addons[__package__].preferences
        prefs.cloud_api_key = secrets.token_urlsafe(24)
        self.report({"INFO"}, "Generated new API Key")
        return {"FINISHED"}


class _BLMCP_OT_cloud_copy_url(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.cloud_copy_url"
    bl_label = "Copy Public URL"
    bl_description = "Copy the active Public URL to clipboard"

    def execute(self, context: bpy.types.Context) -> set[str]:
        pub_url = cloud_bridge.get_public_url()
        if pub_url:
            context.window_manager.clipboard = pub_url
            self.report({"INFO"}, "Public URL copied to clipboard!")
        else:
            self.report({"WARNING"}, "No public URL active yet")
        return {"FINISHED"}


class _BLMCP_OT_cloud_copy_key(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.cloud_copy_key"
    bl_label = "Copy API Key"
    bl_description = "Copy the Agent API Key to clipboard"

    def execute(self, context: bpy.types.Context) -> set[str]:
        prefs = context.preferences.addons[__package__].preferences
        if prefs.cloud_api_key:
            context.window_manager.clipboard = prefs.cloud_api_key
            self.report({"INFO"}, "API Key copied to clipboard!")
        else:
            self.report({"WARNING"}, "API Key is empty")
        return {"FINISHED"}


class _BLMCP_OT_cloud_copy_agent_prompt(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.cloud_copy_agent_prompt"
    bl_label = "Copy Agent Config"
    bl_description = "Copy ready-to-paste connection instructions for your cloud assistant"

    def execute(self, context: bpy.types.Context) -> set[str]:
        prefs = context.preferences.addons[__package__].preferences
        pub_url = cloud_bridge.get_public_url()
        key = prefs.cloud_api_key
        if not pub_url:
            self.report({"WARNING"}, "No public URL active yet")
            return {"CANCELLED"}

        prompt = (
            f"Here are my Blender MCP connection details:\n"
            f"- MCP SSE URL: {pub_url}/sse\n"
            f"- MCP POST URL: {pub_url}/mcp\n"
            f"- REST Endpoint: {pub_url}/execute\n"
            f"- Authorization Token: {key}\n"
            f"Header: Authorization: Bearer {key}\n"
            f"Header: ngrok-skip-browser-warning: 1\n"
        )
        context.window_manager.clipboard = prompt
        self.report({"INFO"}, "Agent configuration copied to clipboard!")
        return {"FINISHED"}


class _BLMCP_OT_cloud_open_ngrok_signup(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.open_ngrok_signup"
    bl_label = "Get Free ngrok Token"
    bl_description = "Open ngrok dashboard in browser to get your free authtoken"

    def execute(self, context: bpy.types.Context) -> set[str]:
        del context
        bpy.ops.wm.url_open(url="https://dashboard.ngrok.com/get-started/your-authtoken")
        return {"FINISHED"}


class _BLMCP_OT_cloud_download_binary(bpy.types.Operator):  # type: ignore[misc]
    bl_idname = "blmcp.download_binary"
    bl_label = "Download Tunnel Binary"
    bl_description = "Automatically download the required tunnel executable"

    binary_name: StringProperty(name="Binary Name", default="ngrok")  # type: ignore[valid-type]

    def execute(self, context: bpy.types.Context) -> set[str]:
        del context
        self.report({"INFO"}, f"Downloading {self.binary_name}... please wait")
        success, msg = cloud_bridge.download_binary(self.binary_name)
        if success:
            self.report({"INFO"}, msg)
            return {"FINISHED"}
        else:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}


# ---------------------------------------------------------------------------
# 3D Viewport Sidebar Panel (N-Panel)
# ---------------------------------------------------------------------------

class _VIEW3D_PT_blender_mcp_panel(bpy.types.Panel):  # type: ignore[misc]
    bl_label = "Blender MCP"
    bl_idname = "VIEW3D_PT_blender_mcp"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        prefs = context.preferences.addons[__package__].preferences

        # Local Server
        box_local = layout.box()
        box_local.label(text="Local MCP Server", icon="PREFERENCES")
        if mcp_to_blender_server.is_running():
            box_local.operator("blmcp.server_stop", icon="CANCEL", text="Stop Server")
            box_local.label(text=f"Running ({prefs.host}:{prefs.port})", icon="CHECKMARK")
        else:
            box_local.operator("blmcp.server_start", icon="PLAY", text="Start Server")
            box_local.label(text="Stopped", icon="X")

        # Cloud Bridge
        box_cloud = layout.box()
        box_cloud.label(text="Cloud Assistant Bridge", icon="WORLD")
        box_cloud.prop(prefs, "tunnel_provider", text="")

        if prefs.tunnel_provider == "ngrok" and not prefs.ngrok_authtoken:
            box_cloud.prop(prefs, "ngrok_authtoken")

        bridge_running = cloud_bridge.is_running()
        pub_url = cloud_bridge.get_public_url()

        if bridge_running:
            box_cloud.operator("blmcp.cloud_stop", icon="CANCEL", text="Stop Bridge")
            if pub_url:
                box_cloud.label(text="Tunnel: Connected", icon="CHECKMARK")
                row_url = box_cloud.row(align=True)
                row_url.operator("blmcp.cloud_copy_url", text="Copy URL", icon="COPYDOWN")
                row_url.operator("blmcp.cloud_copy_agent_prompt", text="Copy Config", icon="TEXT")
                row_key = box_cloud.row(align=True)
                row_key.operator("blmcp.cloud_copy_key", text="Copy API Key", icon="KEYINGSET")
            else:
                box_cloud.label(text="Connecting tunnel...", icon="TIME")
        else:
            box_cloud.operator("blmcp.cloud_start", icon="PLAY", text="Start Cloud Bridge")
            box_cloud.label(text="Bridge Stopped", icon="X")


def _autostart_timer() -> None:
    from . import execute_interactive
    if not _State.startup_online_ok_or_error():
        return
    prefs = bpy.context.preferences.addons[__package__].preferences
    mcp_to_blender_server.timer_internal_vars_calc(
        active=prefs.timer_interval_active,
        idle=prefs.timer_interval_idle,
        idle_delay=prefs.timer_interval_idle_delay,
    )
    mcp_to_blender_server.use_log = prefs.use_log

    if mcp_to_blender_server.is_running():
        return

    try:
        mcp_to_blender_server.start(prefs.host, prefs.port)
    except Exception as ex:
        _State.startup_info_set_from_exception(ex)
        return

    bpy.app.timers.register(
        execute_interactive.run,
        first_interval=mcp_to_blender_server.TIMER_INTERVAL_ACTIVE,
        persistent=True)


def _cli_execute_handler(argv: list[str]) -> int:
    if not _State.startup_online_ok_or_error():
        return 1
    from .cli import cli_execute
    return cli_execute(argv)


_classes = (
    _BlenderMCPPreferences,
    _BLMCP_OT_server_start,
    _BLMCP_OT_server_stop,
    _BLMCP_OT_cloud_start,
    _BLMCP_OT_cloud_stop,
    _BLMCP_OT_cloud_generate_key,
    _BLMCP_OT_cloud_copy_url,
    _BLMCP_OT_cloud_copy_key,
    _BLMCP_OT_cloud_copy_agent_prompt,
    _BLMCP_OT_cloud_open_ngrok_signup,
    _BLMCP_OT_cloud_download_binary,
    _VIEW3D_PT_blender_mcp_panel,
)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    _cli_commands.append(bpy.utils.register_cli_command("blender_mcp", _cli_execute_handler))

    if not bpy.app.background:
        if not _State.startup_online_ok_or_error():
            return
        prefs = bpy.context.preferences.addons[__package__].preferences
        if prefs.use_autostart:
            bpy.app.timers.register(
                _autostart_timer,
                first_interval=prefs.autostart_delay,
                persistent=True,
            )


def unregister() -> None:
    from . import execute_interactive

    cloud_bridge.stop()

    for cmd in _cli_commands:
        bpy.utils.unregister_cli_command(cmd)
    _cli_commands.clear()

    if bpy.app.timers.is_registered(_autostart_timer):
        bpy.app.timers.unregister(_autostart_timer)

    mcp_to_blender_server.stop()
    if bpy.app.timers.is_registered(execute_interactive.run):
        bpy.app.timers.unregister(execute_interactive.run)
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
