# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Cloud Bridge for Blender MCP.
Runs a background HTTP/SSE/MCP server and manages either ngrok or Cloudflare Tunnel
to securely expose Blender MCP to cloud AI assistants.
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

VERSION = "1.0.4"

# ---------------------------------------------------------------------------
# Global State for the embedded bridge
# ---------------------------------------------------------------------------

class _CloudBridgeState:
    httpd: Optional[http.server.ThreadingHTTPServer] = None
    server_thread: Optional[threading.Thread] = None
    tunnel_proc: Optional[subprocess.Popen] = None
    tunnel_thread: Optional[threading.Thread] = None
    public_url: str = ""
    api_key: str = ""
    blender_host: str = "127.0.0.1"
    blender_port: int = 9876
    http_port: int = 8765
    timeout: float = 120.0
    status_message: str = "Stopped"
    is_running: bool = False
    
    # SSE sessions: session_id -> queue.Queue
    sse_sessions: Dict[str, queue.Queue] = {}
    sse_lock: threading.Lock = threading.Lock()


_state = _CloudBridgeState()


def get_binary_install_dir() -> Path:
    """Returns preferred directory for downloaded binaries."""
    dest = Path.home() / ".blender-mcp"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def find_executable(name: str) -> Optional[Path]:
    r"""Finds binary in addon folder, user home (.blender-mcp), C:\Blender MCP, or system PATH."""
    candidates = [
        Path(__file__).resolve().parent / f"{name}.exe",
        Path(__file__).resolve().parent / name,
        get_binary_install_dir() / f"{name}.exe",
        get_binary_install_dir() / name,
        Path(f"C:/Blender MCP/{name}.exe"),
        Path(f"C:/Blender MCP/{name}"),
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return p

    which_path = shutil.which(name)
    if which_path:
        return Path(which_path)

    return None


def download_binary(name: str) -> Tuple[bool, str]:
    """Downloads and extracts ngrok or cloudflared into the user directory."""
    dest_dir = get_binary_install_dir()
    
    if name == "ngrok":
        url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
        zip_path = dest_dir / "ngrok.zip"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BlenderMCP/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(zip_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(dest_dir)
            try:
                zip_path.unlink()
            except Exception:
                pass
            return True, f"ngrok successfully installed to {dest_dir}"
        except Exception as e:
            return False, f"Failed to download ngrok: {e}"
            
    elif name == "cloudflared":
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        exe_path = dest_dir / "cloudflared.exe"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BlenderMCP/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp, open(exe_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            return True, f"cloudflared successfully installed to {dest_dir}"
        except Exception as e:
            return False, f"Failed to download cloudflared: {e}"

    return False, f"Unknown binary requested: {name}"


# ---------------------------------------------------------------------------
# Blender Socket Client
# ---------------------------------------------------------------------------

def send_to_blender(
    code: str,
    blender_host: str = "127.0.0.1",
    blender_port: int = 9876,
    strict_json: bool = False,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    # Force IPv4 127.0.0.1 to avoid Windows IPv6 (::1) localhost resolution timeouts
    if blender_host.lower() == "localhost":
        blender_host = "127.0.0.1"

    payload = {
        "type": "execute",
        "code": code,
        "strict_json": strict_json,
    }
    req_bytes = json.dumps(payload).encode("utf-8") + b"\0"

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((blender_host, blender_port))
    except (ConnectionRefusedError, OSError) as e:
        return {
            "status": "error",
            "message": f"Could not connect to Blender MCP server at {blender_host}:{blender_port}. Is the local server started? Error: {e}",
            "stdout": "",
            "stderr": "",
        }

    try:
        sock.sendall(req_bytes)
        buf = bytearray()
        while b"\0" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)

        if not buf:
            return {
                "status": "error",
                "message": "Blender closed connection without sending a response.",
                "stdout": "",
                "stderr": "",
            }

        idx = buf.index(b"\0") if b"\0" in buf else len(buf)
        resp_data = bytes(buf[:idx]).decode("utf-8")
        return json.loads(resp_data)
    except socket.timeout:
        return {
            "status": "error",
            "message": f"Execution timed out after {timeout} seconds.",
            "stdout": "",
            "stderr": "",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error communicating with Blender MCP server: {e}",
            "stdout": "",
            "stderr": "",
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass


def check_blender_connection(host: str = "127.0.0.1", port: int = 9876) -> bool:
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect((host, port))
        sock.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# MCP Protocol Handlers
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "execute_blender_code",
        "description": "Execute arbitrary Python code inside the active Blender instance and return results, captured stdout, and any errors. Full access to the `bpy` module.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute in Blender. You can assign a dict to `result` (e.g. `result = {'status': 'done'}`) to return structured data."
                },
                "strict_json": {
                    "type": "boolean",
                    "description": "If true, result must strictly be JSON serializable. If false, non-serializable objects fall back to repr.",
                    "default": False
                }
            },
            "required": ["code"]
        }
    },
    {
        "name": "get_blender_scene_info",
        "description": "Inspect the active Blender scene and return summary metadata (scene name, render engine, active object, object list with locations, materials, collections, current frame).",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]

SCENE_INFO_CODE = """
import bpy
res = {
    "scene": bpy.context.scene.name,
    "render_engine": bpy.context.scene.render.engine,
    "active_object": bpy.context.active_object.name if bpy.context.active_object else None,
    "selected_objects": [o.name for o in bpy.context.selected_objects],
    "objects_count": len(bpy.context.scene.objects),
    "objects": [
        {
            "name": o.name,
            "type": o.type,
            "location": [round(v, 4) for v in o.location],
            "visible": not o.hide_viewport
        }
        for o in bpy.context.scene.objects[:100]
    ],
    "materials": [m.name for m in bpy.data.materials[:50]],
    "collections": [c.name for c in bpy.data.collections],
    "timeline": {
        "current": bpy.context.scene.frame_current,
        "start": bpy.context.scene.frame_start,
        "end": bpy.context.scene.frame_end,
        "fps": bpy.context.scene.render.fps
    }
}
result = res
"""

def handle_mcp_request(
    msg: Dict[str, Any],
    blender_host: str,
    blender_port: int,
    timeout: float,
) -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    req_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "blender-mcp-cloud-bridge",
                    "version": VERSION
                }
            }
        }

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}

    if method == "tools/call":
        params = msg.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "execute_blender_code":
            code = args.get("code", "")
            strict_json = args.get("strict_json", False)
            res = send_to_blender(code, blender_host, blender_port, strict_json, timeout)
            is_err = res.get("status") == "error"
            text_parts = []
            if is_err:
                text_parts.append(f"Error: {res.get('message')}")
            else:
                text_parts.append(f"Status: ok\nResult: {json.dumps(res.get('result', {}), indent=2)}")
            if res.get("stdout"):
                text_parts.append(f"Stdout:\n{res.get('stdout')}")
            if res.get("stderr"):
                text_parts.append(f"Stderr:\n{res.get('stderr')}")

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "\n\n".join(text_parts)}],
                    "isError": is_err,
                }
            }

        elif tool_name == "get_blender_scene_info":
            res = send_to_blender(SCENE_INFO_CODE, blender_host, blender_port, False, timeout)
            is_err = res.get("status") == "error"
            text = f"Error: {res.get('message')}" if is_err else json.dumps(res.get("result", {}), indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": is_err,
                }
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {tool_name}"}
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unsupported method: {method}"}
    }


# ---------------------------------------------------------------------------
# HTTP & SSE Request Handler
# ---------------------------------------------------------------------------

class _BridgeHTTPHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"BlenderMCPBridge/{VERSION}"

    def send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key, Last-Event-ID, ngrok-skip-browser-warning")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if secrets.compare_digest(token, _state.api_key):
                return True

        x_key = self.headers.get("X-API-Key", "").strip()
        if x_key and secrets.compare_digest(x_key, _state.api_key):
            return True

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        q_key = params.get("api_key", [""])[0] or params.get("token", [""])[0]
        if q_key and secrets.compare_digest(q_key, _state.api_key):
            return True

        return False

    def send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_unauthorized(self) -> None:
        self.send_json(401, {
            "status": "error",
            "error": "Unauthorized",
            "message": "Missing or invalid API key. Provide via 'Authorization: Bearer <KEY>' header, 'X-API-Key: <KEY>', or '?api_key=<KEY>' query param."
        })

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/health":
            blender_ok = check_blender_connection(_state.blender_host, _state.blender_port)
            self.send_json(200, {
                "status": "online",
                "blender_connected": blender_ok,
                "blender_target": f"{_state.blender_host}:{_state.blender_port}",
                "public_url": _state.public_url or f"http://localhost:{self.server.server_port}",
                "version": VERSION
            })
            return

        if path == "/openapi.json":
            self.send_openapi_spec()
            return

        if path == "/":
            self.send_dashboard()
            return

        if not self.check_auth():
            self.send_unauthorized()
            return

        if path == "/sse":
            self.handle_sse_stream()
            return

        self.send_json(404, {"error": "Not Found", "path": path})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self.check_auth():
            self.send_unauthorized()
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else b""

        if path in ("/execute", "/run"):
            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception as e:
                self.send_json(400, {"status": "error", "message": f"Invalid JSON body: {e}"})
                return

            code = body.get("code")
            if not code or not isinstance(code, str):
                self.send_json(400, {
                    "status": "error",
                    "message": "Request body must contain 'code' string."
                })
                return

            strict_json = body.get("strict_json", False)
            res = send_to_blender(
                code=code,
                blender_host=_state.blender_host,
                blender_port=_state.blender_port,
                strict_json=strict_json,
                timeout=_state.timeout,
            )
            http_status = 200 if res.get("status") == "ok" else 400
            self.send_json(http_status, res)
            return

        if path == "/mcp":
            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
            except Exception as e:
                self.send_json(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": f"Parse error: {e}"}})
                return

            resp = handle_mcp_request(
                req_json,
                blender_host=_state.blender_host,
                blender_port=_state.blender_port,
                timeout=_state.timeout,
            )
            if resp is not None:
                self.send_json(200, resp)
            else:
                self.send_response(204)
                self.send_cors_headers()
                self.end_headers()
            return

        if path == "/messages":
            params = urllib.parse.parse_qs(parsed.query)
            session_id = params.get("session_id", [""])[0]

            with _state.sse_lock:
                q = _state.sse_sessions.get(session_id)

            if not q:
                self.send_json(404, {"error": "Invalid or expired SSE session_id"})
                return

            try:
                req_json = json.loads(body_bytes.decode("utf-8"))
            except Exception as e:
                self.send_json(400, {"error": f"Invalid JSON: {e}"})
                return

            resp = handle_mcp_request(
                req_json,
                blender_host=_state.blender_host,
                blender_port=_state.blender_port,
                timeout=_state.timeout,
            )
            if resp is not None:
                q.put(resp)

            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_cors_headers()
            self.end_headers()
            return

        self.send_json(404, {"error": "Not Found", "path": path})

    def handle_sse_stream(self) -> None:
        session_id = secrets.token_hex(16)
        msg_queue: queue.Queue = queue.Queue()

        with _state.sse_lock:
            _state.sse_sessions[session_id] = msg_queue

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_cors_headers()
        self.end_headers()

        endpoint_event = f"event: endpoint\ndata: /messages?session_id={session_id}\n\n"
        self.wfile.write(endpoint_event.encode("utf-8"))
        self.wfile.flush()

        try:
            while True:
                try:
                    item = msg_queue.get(timeout=15.0)
                    data_str = json.dumps(item)
                    msg_event = f"event: message\ndata: {data_str}\n\n"
                    self.wfile.write(msg_event.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _state.sse_lock:
                _state.sse_sessions.pop(session_id, None)

    def send_openapi_spec(self) -> None:
        server_url = _state.public_url or f"http://localhost:{self.server.server_port}"
        spec = {
            "openapi": "3.0.3",
            "info": {
                "title": "Blender MCP Cloud Bridge API",
                "description": "API allowing cloud AI assistants to interactively control Blender via Python scripts.",
                "version": VERSION
            },
            "servers": [{"url": server_url}],
            "paths": {
                "/execute": {
                    "post": {
                        "summary": "Execute Python code in Blender",
                        "description": "Runs Python code inside the active Blender instance with full access to `bpy`.",
                        "operationId": "executeBlenderCode",
                        "security": [{"BearerAuth": []}, {"ApiKeyAuth": []}],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "code": {"type": "string"},
                                            "strict_json": {"type": "boolean", "default": False}
                                        },
                                        "required": ["code"]
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "Execution successful",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "status": {"type": "string"},
                                                "result": {"type": "object"},
                                                "stdout": {"type": "string"},
                                                "stderr": {"type": "string"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "securitySchemes": {
                    "BearerAuth": {"type": "http", "scheme": "bearer"},
                    "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
                }
            }
        }
        self.send_json(200, spec)

    def send_dashboard(self) -> None:
        blender_ok = check_blender_connection(_state.blender_host, _state.blender_port)
        pub_url = _state.public_url or f"http://localhost:{self.server.server_port}"
        key = _state.api_key
        status_color = "#10b981" if blender_ok else "#ef4444"
        status_text = "Connected" if blender_ok else "Disconnected (Start Blender MCP)"

        html = f"""<!DOCTYPE html>
<html>
<head>
<title>Blender MCP Cloud Bridge</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 2rem; }}
  .container {{ max-width: 800px; margin: 0 auto; }}
  h1 {{ margin-top: 0; color: #38bdf8; }}
  .badge {{ font-size: 0.85rem; padding: 0.25rem 0.6rem; border-radius: 9999px; background: #1e293b; color: #94a3b8; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; border: 1px solid #334155; }}
  .status {{ font-weight: 600; color: {status_color}; }}
  pre {{ background: #090d16; padding: 1rem; border-radius: 8px; overflow-x: auto; color: #38bdf8; font-size: 0.9rem; }}
  code {{ font-family: ui-monospace, Menlo, Monaco, Consolas, monospace; }}
  .kv {{ display: grid; grid-template-columns: 140px 1fr; gap: 0.75rem; margin: 1rem 0; }}
  .label {{ color: #94a3b8; }}
</style>
</head>
<body>
<div class="container">
  <h1>Blender MCP Cloud Bridge <span class="badge">v{VERSION}</span></h1>
  <div class="card">
    <h3>Bridge Status</h3>
    <div class="kv">
      <div class="label">Blender MCP:</div>
      <div class="status">{status_text} ({_state.blender_host}:{_state.blender_port})</div>
      <div class="label">Public URL:</div>
      <div><code>{pub_url}</code></div>
      <div class="label">API Key:</div>
      <div><code>{key}</code></div>
    </div>
  </div>
  <div class="card">
    <h3>REST API Example</h3>
    <pre><code>curl -X POST "{pub_url}/execute" \\
  -H "Authorization: Bearer {key}" \\
  -H "Content-Type: application/json" \\
  -d '{{"code": "import bpy\\nbpy.ops.mesh.primitive_monkey_add()\\nresult={{\\\"created\\\": \\\"Suzanne\\\"}}"}}'</code></pre>
  </div>
  <div class="card">
    <h3>MCP SSE Configuration</h3>
    <pre><code>{{
  "mcpServers": {{
    "blender": {{
      "url": "{pub_url}/sse",
      "headers": {{"Authorization": "Bearer {key}"}}
    }}
  }}
}}</code></pre>
  </div>
</div>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Public Bridge Controller API
# ---------------------------------------------------------------------------

def is_running() -> bool:
    return _state.is_running


def get_public_url() -> str:
    return _state.public_url


def get_status_message() -> str:
    return _state.status_message


def start(
    blender_host: str = "127.0.0.1",
    blender_port: int = 9876,
    http_port: int = 8765,
    api_key: str = "",
    ngrok_authtoken: str = "",
    timeout: float = 120.0,
    tunnel_provider: str = "ngrok",  # "ngrok" or "cloudflare"
    on_url_ready: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    if _state.is_running:
        return True, _state.public_url

    if not api_key:
        api_key = secrets.token_urlsafe(24)

    _state.blender_host = "127.0.0.1" if blender_host.lower() == "localhost" else blender_host
    _state.blender_port = blender_port
    _state.http_port = http_port
    _state.api_key = api_key
    _state.timeout = timeout
    _state.public_url = ""
    _state.status_message = "Starting HTTP server..."

    try:
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", http_port), _BridgeHTTPHandler)
        _state.httpd = httpd
    except OSError as e:
        _state.status_message = f"Port {http_port} in use: {e}"
        return False, _state.status_message

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    _state.server_thread = server_thread
    _state.is_running = True

    # Tunnel Management
    if tunnel_provider == "ngrok":
        bin_path = find_executable("ngrok")
        if not bin_path:
            _state.public_url = f"http://127.0.0.1:{http_port}"
            _state.status_message = "Running (ngrok binary not found - click Download ngrok)"
            return True, _state.public_url

        _state.status_message = "Connecting ngrok tunnel..."

        def _run_ngrok() -> None:
            # Bind strictly to IPv4 127.0.0.1
            cmd = [str(bin_path), "http", f"127.0.0.1:{http_port}", "--log=stdout"]
            if ngrok_authtoken and ngrok_authtoken.strip():
                cmd.extend(["--authtoken", ngrok_authtoken.strip()])

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                )
                _state.tunnel_proc = proc
            except Exception as e:
                _state.status_message = f"Failed to launch ngrok: {e}"
                return

            # Wait for ngrok local API to expose the public URL
            start_t = time.time()
            while time.time() - start_t < 15.0:
                if proc.poll() is not None:
                    _state.status_message = f"ngrok stopped early ({proc.returncode})"
                    break
                try:
                    req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels")
                    with urllib.request.urlopen(req, timeout=1) as resp:
                        data = json.loads(resp.read().decode())
                        for t in data.get("tunnels", []):
                            u = t.get("public_url", "")
                            if u.startswith("https://"):
                                _state.public_url = u
                                _state.status_message = f"Active: {u}"
                                if on_url_ready:
                                    try:
                                        on_url_ready(u)
                                    except Exception:
                                        pass
                                return
                except Exception:
                    pass
                time.sleep(0.5)

        tunnel_thread = threading.Thread(target=_run_ngrok, daemon=True)
        tunnel_thread.start()
        _state.tunnel_thread = tunnel_thread

    else:
        # Cloudflare Tunnel
        bin_path = find_executable("cloudflared")
        if not bin_path:
            _state.public_url = f"http://127.0.0.1:{http_port}"
            _state.status_message = "Running (cloudflared not found - click Download cloudflared)"
            return True, _state.public_url

        _state.status_message = "Connecting Cloudflare Tunnel..."

        def _run_cf() -> None:
            cmd = [str(bin_path), "tunnel", "--url", f"http://127.0.0.1:{http_port}", "--no-autoupdate"]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                )
                _state.tunnel_proc = proc
            except Exception as e:
                _state.status_message = f"Failed to launch cloudflared: {e}"
                return

            url_regex = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
            for line in iter(proc.stdout.readline, ""):
                match = url_regex.search(line)
                if match:
                    _state.public_url = match.group(0)
                    _state.status_message = f"Active: {_state.public_url}"
                    if on_url_ready:
                        try:
                            on_url_ready(_state.public_url)
                        except Exception:
                            pass
                    break
                if proc.poll() is not None:
                    _state.status_message = f"cloudflared stopped ({proc.returncode})"
                    break

        tunnel_thread = threading.Thread(target=_run_cf, daemon=True)
        tunnel_thread.start()
        _state.tunnel_thread = tunnel_thread

    # Wait up to 10s for URL
    start_t = time.time()
    while time.time() - start_t < 10.0:
        if _state.public_url:
            break
        time.sleep(0.5)

    return True, _state.public_url or "Tunnel starting in background..."


def stop() -> None:
    _state.is_running = False
    _state.status_message = "Stopped"
    _state.public_url = ""

    if _state.tunnel_proc:
        try:
            _state.tunnel_proc.terminate()
            _state.tunnel_proc.wait(timeout=2)
        except Exception:
            try:
                _state.tunnel_proc.kill()
            except Exception:
                pass
        _state.tunnel_proc = None

    if _state.httpd:
        try:
            _state.httpd.shutdown()
            _state.httpd.server_close()
        except Exception:
            pass
        _state.httpd = None
