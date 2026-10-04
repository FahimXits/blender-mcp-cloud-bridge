"""
Blender MCP Cloud Bridge
========================
Provides a secure public bridge for Blender MCP.
Supports both Cloudflare Tunnel and ngrok.
Exposes:
  1. REST API (POST /execute, POST /run, GET /health, GET /openapi.json)
  2. MCP protocol over HTTP (SSE: GET /sse & POST /messages, and Streamable HTTP: POST /mcp)

Zero external dependencies - runs on standard Python 3.10+.
"""

import argparse
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
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

VERSION = "1.0.4"
CONFIG_FILE = Path(__file__).resolve().parent / "bridge_config.json"


def find_executable(name: str) -> Optional[Path]:
    """Finds binary in script directory, user home (.blender-mcp), or system PATH."""
    candidates = [
        Path(__file__).resolve().parent / f"{name}.exe",
        Path(__file__).resolve().parent / name,
        Path.home() / ".blender-mcp" / f"{name}.exe",
        Path.home() / ".blender-mcp" / name,
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return p

    which_path = shutil.which(name)
    if which_path:
        return Path(which_path)

    return None


# ---------------------------------------------------------------------------
# Configuration Management
# ---------------------------------------------------------------------------

def load_or_create_config() -> Dict[str, Any]:
    default_config = {
        "api_key": secrets.token_urlsafe(24),
        "http_port": 8765,
        "blender_host": "127.0.0.1",
        "blender_port": 9876,
        "request_timeout_seconds": 120,
        "tunnel_provider": "cloudflare",  # "cloudflare" or "ngrok"
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_config.update(saved)
        except Exception as e:
            print(f"[Bridge] Warning: Failed to read {CONFIG_FILE}: {e}", flush=True)

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2)
    except Exception as e:
        print(f"[Bridge] Warning: Failed to write {CONFIG_FILE}: {e}", flush=True)

    return default_config


# ---------------------------------------------------------------------------
# Blender Socket Communication
# ---------------------------------------------------------------------------

def send_to_blender(
    code: str,
    blender_host: str = "127.0.0.1",
    blender_port: int = 9876,
    strict_json: bool = False,
    timeout: float = 120.0,
) -> Dict[str, Any]:
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
            "message": f"Could not connect to Blender MCP server at {blender_host}:{blender_port}. Is Blender running and the MCP add-on started? Error: {e}",
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
                "message": "Blender closed the connection without sending a response.",
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
            "message": f"Error communicating with Blender: {e}",
            "stdout": "",
            "stderr": "",
        }
    finally:
        try:
            sock.close()
        except Exception:
            pass


def check_blender_connection(host: str = "127.0.0.1", port: int = 9876) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect((host, port))
        sock.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# MCP Protocol Definitions
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
            text = f"Error querying scene info: {res.get('message')}" if is_err else json.dumps(res.get("result", {}), indent=2)
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

class BridgeContext:
    api_key: str = ""
    blender_host: str = "127.0.0.1"
    blender_port: int = 9876
    timeout: float = 120.0
    public_url: str = ""
    sse_sessions: Dict[str, queue.Queue] = {}
    sse_lock: threading.Lock = threading.Lock()


class BridgeHTTPHandler(http.server.BaseHTTPRequestHandler):
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
            if secrets.compare_digest(token, BridgeContext.api_key):
                return True

        x_key = self.headers.get("X-API-Key", "").strip()
        if x_key and secrets.compare_digest(x_key, BridgeContext.api_key):
            return True

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        q_key = params.get("api_key", [""])[0] or params.get("token", [""])[0]
        if q_key and secrets.compare_digest(q_key, BridgeContext.api_key):
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
            blender_ok = check_blender_connection(BridgeContext.blender_host, BridgeContext.blender_port)
            self.send_json(200, {
                "status": "online",
                "blender_connected": blender_ok,
                "blender_target": f"{BridgeContext.blender_host}:{BridgeContext.blender_port}",
                "public_url": BridgeContext.public_url or f"http://localhost:{self.server.server_port}",
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
                blender_host=BridgeContext.blender_host,
                blender_port=BridgeContext.blender_port,
                strict_json=strict_json,
                timeout=BridgeContext.timeout,
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
                blender_host=BridgeContext.blender_host,
                blender_port=BridgeContext.blender_port,
                timeout=BridgeContext.timeout,
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

            with BridgeContext.sse_lock:
                q = BridgeContext.sse_sessions.get(session_id)

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
                blender_host=BridgeContext.blender_host,
                blender_port=BridgeContext.blender_port,
                timeout=BridgeContext.timeout,
            )
            if resp is not None:
                q.put(resp)

            self.send_response(202)
            self.send_cors_headers()
            self.end_headers()
            return

        self.send_json(404, {"error": "Not Found", "path": path})

    def handle_sse_stream(self) -> None:
        session_id = secrets.token_hex(16)
        msg_queue: queue.Queue = queue.Queue()

        with BridgeContext.sse_lock:
            BridgeContext.sse_sessions[session_id] = msg_queue

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
            with BridgeContext.sse_lock:
                BridgeContext.sse_sessions.pop(session_id, None)

    def send_openapi_spec(self) -> None:
        server_url = BridgeContext.public_url or f"http://localhost:{self.server.server_port}"
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
        blender_ok = check_blender_connection(BridgeContext.blender_host, BridgeContext.blender_port)
        pub_url = BridgeContext.public_url or f"http://localhost:{self.server.server_port}"
        key = BridgeContext.api_key
        status_color = "#10b981" if blender_ok else "#ef4444"
        status_text = "Connected" if blender_ok else "Disconnected (Check Blender Add-on)"

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
      <div class="status">{status_text} ({BridgeContext.blender_host}:{BridgeContext.blender_port})</div>
      <div class="label">Public URL:</div>
      <div><code>{pub_url}</code></div>
      <div class="label">API Key:</div>
      <div><code>{key}</code></div>
    </div>
  </div>
  <div class="card">
    <h3>cURL Example</h3>
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
        sys.stderr.write(f"[HTTP] {self.address_string()} - {format % args}\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Tunnel Providers
# ---------------------------------------------------------------------------

class CloudflareTunnel:
    def __init__(self, port: int):
        self.port = port
        self.bin = find_executable("cloudflared")
        self.process: Optional[subprocess.Popen] = None
        self.public_url: Optional[str] = None
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def start(self) -> Optional[str]:
        if not self.bin:
            print("[Tunnel] Error: cloudflared not found.", flush=True)
            return None

        cmd = [str(self.bin), "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate"]
        print(f"[Tunnel] Starting Cloudflare Tunnel for port {self.port}...", flush=True)
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except Exception as e:
            print(f"[Tunnel] Failed to start cloudflared: {e}", flush=True)
            return None

        url_regex = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        start_time = time.time()
        for line in iter(self.process.stdout.readline, ""):
            match = url_regex.search(line)
            if match:
                self.public_url = match.group(0)
                break
            if time.time() - start_time > 35.0 or self.process.poll() is not None:
                break

        if self.public_url:
            self._monitor_thread = threading.Thread(target=self._drain, daemon=True)
            self._monitor_thread.start()
            return self.public_url
        return None

    def _drain(self) -> None:
        if self.process and self.process.stdout:
            for _ in iter(self.process.stdout.readline, ""):
                if self._stop_event.is_set():
                    break

    def stop(self) -> None:
        self._stop_event.set()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None


class NgrokTunnel:
    def __init__(self, port: int):
        self.port = port
        self.bin = find_executable("ngrok")
        self.process: Optional[subprocess.Popen] = None
        self.public_url: Optional[str] = None

    def start(self) -> Optional[str]:
        if not self.bin:
            print("[Tunnel] Error: ngrok executable not found.", flush=True)
            return None

        cmd = [str(self.bin), "http", f"127.0.0.1:{self.port}", "--log=stdout"]
        print(f"[Tunnel] Starting ngrok on port {self.port} (IPv4 127.0.0.1)...", flush=True)
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except Exception as e:
            print(f"[Tunnel] Failed to start ngrok: {e}", flush=True)
            return None

        # Query local ngrok API to get public URL
        start_t = time.time()
        while time.time() - start_t < 15.0:
            if self.process.poll() is not None:
                print(f"[Tunnel] ngrok exited early. Did you add your authtoken? (run: .\\ngrok.exe config add-authtoken <token>)", flush=True)
                return None
            try:
                req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels")
                with urllib.request.urlopen(req, timeout=1) as resp:
                    data = json.loads(resp.read().decode())
                    tunnels = data.get("tunnels", [])
                    for t in tunnels:
                        p_url = t.get("public_url", "")
                        if p_url.startswith("https://"):
                            self.public_url = p_url
                            return self.public_url
            except Exception:
                pass
            time.sleep(0.5)

        return None

    def stop(self) -> None:
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_bridge(tunnel_type: str = "cloudflare", no_tunnel: bool = False) -> None:
    config = load_or_create_config()
    BridgeContext.api_key = config["api_key"]
    BridgeContext.blender_host = config.get("blender_host", "127.0.0.1")
    BridgeContext.blender_port = config.get("blender_port", 9876)
    BridgeContext.timeout = float(config.get("request_timeout_seconds", 120))
    http_port = int(config.get("http_port", 8765))

    server_address = ("0.0.0.0", http_port)
    try:
        httpd = http.server.ThreadingHTTPServer(server_address, BridgeHTTPHandler)
    except OSError as e:
        print(f"[Bridge] Error: Port {http_port} is already in use. Error: {e}", flush=True)
        return

    print("=" * 70, flush=True)
    print(f" BLENDER MCP CLOUD BRIDGE v{VERSION}", flush=True)
    print("=" * 70, flush=True)
    print(f" Local HTTP Bridge : http://127.0.0.1:{http_port}", flush=True)
    print(f" Target Blender MCP: {BridgeContext.blender_host}:{BridgeContext.blender_port}", flush=True)
    print(f" API Key           : {BridgeContext.api_key}", flush=True)
    print("=" * 70, flush=True)

    tunnel_obj = None
    if not no_tunnel:
        if tunnel_type == "ngrok":
            tunnel_obj = NgrokTunnel(http_port)
        else:
            tunnel_obj = CloudflareTunnel(http_port)

        pub_url = tunnel_obj.start()
        if pub_url:
            BridgeContext.public_url = pub_url
            print("\n" + "=" * 70, flush=True)
            print(f" >>> PUBLIC CLOUD URL: {pub_url} <<<", flush=True)
            print(f" >>> API KEY         : {BridgeContext.api_key} <<<", flush=True)
            print("=" * 70, flush=True)
            print("To connect your cloud assistant:")
            print(f" - REST endpoint : {pub_url}/execute", flush=True)
            print(f" - MCP SSE URL   : {pub_url}/sse", flush=True)
            print(f" - MCP Post URL  : {pub_url}/mcp", flush=True)
            print(f" - OpenAPI spec  : {pub_url}/openapi.json", flush=True)
            print("=" * 70 + "\n", flush=True)
        else:
            print(f"[Bridge] Tunnel could not start. Operating in local mode.", flush=True)

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    print("Bridge is running! Press Ctrl+C to stop.\n", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping bridge...", flush=True)
    finally:
        if tunnel_obj:
            tunnel_obj.stop()
        httpd.shutdown()
        httpd.server_close()
        print("Bridge stopped cleanly.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Blender MCP Cloud Bridge")
    parser.add_argument("--tunnel", choices=["cloudflare", "ngrok"], default=None, help="Tunnel provider to use")
    parser.add_argument("--no-tunnel", action="store_true", help="Run HTTP bridge locally without public tunnel")
    args = parser.parse_args()
    config = load_or_create_config()
    chosen_tunnel = args.tunnel or config.get("tunnel_provider", "ngrok")
    run_bridge(tunnel_type=chosen_tunnel, no_tunnel=args.no_tunnel)
