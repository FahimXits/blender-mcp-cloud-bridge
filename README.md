# Blender MCP Cloud Bridge

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Blender](https://img.shields.io/badge/Blender-4.2%20%7C%205.x-E87D0D?style=flat-square&logo=blender&logoColor=white)](https://www.blender.org/)
[![Protocol](https://img.shields.io/badge/Protocol-MCP%20%2B%20REST-7928ca?style=flat-square)](https://modelcontextprotocol.io/)
[![Cloud Agents](https://img.shields.io/badge/Built%20For-Muse%20AI%20%7C%20OpenClaw%20%7C%20Hermes-00f2fe?style=flat-square)](https://github.com/FahimXits/blender-mcp-cloud-bridge)
[![License](https://img.shields.io/badge/License-GPL--3.0-green?style=flat-square)](LICENSE)

**Engineered for Cloud AI Assistants (Muse AI, OpenClaw, Hermes) & autonomous agents to remotely drive local Blender 3D via Model Context Protocol (MCP) and secure automated tunnels.**

</div>

---

## 🎯 Purpose-Built for Cloud AI Assistants

Most Blender MCP servers are locked to `localhost:9876`. While this works for local desktop apps, **modern autonomous AI agents (Muse AI, OpenClaw, Hermes, cloud LangChain/AutoGPT pipelines) run in remote cloud containers**. They are completely blocked by local NAT, home routers, and corporate firewalls.

**Blender MCP Cloud Bridge** solves this permanently:
* 🌐 **Public HTTPS Edge Gateway:** Automatically provisions a secure public tunnel via **Cloudflare Quick Tunnels** (zero configuration) or **ngrok** (bypasses datacenter IP restrictions).
* 🤖 **Cloud Agent First:** Purpose-built for remote agents running in AWS, GCP, or Docker to send `bpy` commands, inspect active 3D scenes, and trigger renders.
* ⚡ **Zero External Dependencies:** Built entirely with Python's standard library. Zero `pip install` required inside Blender.
* 🔒 **Hardened Bearer Authentication:** Enforces secret API token validation on all inbound execution requests.
* 🖥️ **Universal Client Support:** Connects cloud agents via REST (`POST /execute`) or MCP (`POST /mcp`), plus 1-click integration for desktop assistants like **Claude Desktop** and **Cursor** (`GET /sse`).

---

## ⚡ Architecture Flow

```
┌────────────────────────────────────────────────────────┐
│             Cloud AI Assistants & Frameworks           │
│   • Muse AI (New)        • OpenClaw Agent              │
│   • Hermes Agent         • Cloud LLM Pipelines (AWS)   │
│   • Claude Desktop       • Cursor IDE                  │
└───────────────────────────┬────────────────────────────┘
                            │ HTTPS + Bearer API Key
                            ▼
┌────────────────────────────────────────────────────────┐
│            Secure Tunnel Edge Gateway                  │
│    (Cloudflare Quick Tunnels or ngrok Edge Node)       │
└───────────────────────────┬────────────────────────────┘
                            │ Loopback HTTP (:8765)
                            ▼
┌────────────────────────────────────────────────────────┐
│            Blender MCP Cloud Bridge                    │
│   • Token Auth & Rate Guards  • MCP Protocol Server    │
│   • OpenAPI 3.0 Generator     • REST & SSE Handlers    │
└───────────────────────────┬────────────────────────────┘
                            │ Non-blocking JSON Socket (:9876)
                            ▼
┌────────────────────────────────────────────────────────┐
│               Local Blender 3D Instance                │
│   • Full `bpy` Access         • Weak Sandbox Guard     │
│   • Scene Graph Inspector     • Real-time Output Sync  │
└────────────────────────────────────────────────────────┘
```

---

## 🤖 Connecting Cloud Assistants & Autonomous Agents

### 1. Cloud Agents (Muse AI, OpenClaw, Hermes, Custom Python)

Autonomous cloud agents can execute procedural 3D modeling scripts, create objects, and query geometry over standard authenticated REST:

```bash
curl -X POST "https://YOUR-TUNNEL-URL.trycloudflare.com/execute" \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "code": "import bpy\nbpy.ops.mesh.primitive_monkey_add(location=(0,0,1))\nresult = {\"status\": \"success\", \"object\": bpy.context.active_object.name}"
  }'
```

#### Python Example for Cloud Agent Frameworks:
```python
import requests

BRIDGE_URL = "https://YOUR-TUNNEL-URL.trycloudflare.com/execute"
API_KEY = "YOUR_AGENT_API_KEY"

def instruct_blender(python_code: str) -> dict:
    response = requests.post(
        BRIDGE_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"code": python_code}
    )
    return response.json()

# Example: Ask agent to inspect and build
result = instruct_blender("""
import bpy
bpy.ops.mesh.primitive_cylinder_add(radius=1.5, depth=3.0)
result = {"created": bpy.context.active_object.name, "total_objects": len(bpy.data.objects)}
""")
print(result)
```

---

### 2. Streamable HTTP & SSE MCP (Model Context Protocol)

For cloud agents and MCP clients using native JSON-RPC 2.0:

* **Streamable HTTP Endpoint:** `POST https://YOUR-TUNNEL-URL.trycloudflare.com/mcp`
* **SSE Endpoint:** `GET https://YOUR-TUNNEL-URL.trycloudflare.com/sse`
* **Messages Endpoint:** `POST https://YOUR-TUNNEL-URL.trycloudflare.com/messages?session_id=<ID>`

#### Available MCP Tools:
| Tool Name | Description |
| :--- | :--- |
| `execute_blender_code` | Executes arbitrary Python code with direct access to Blender's `bpy` context. |
| `get_blender_scene_info` | Retrieves comprehensive scene metadata: objects, meshes, materials, collections, render engine, and timeline frame info. |

---

### 3. Desktop Assistants (Claude Desktop & Cursor)

While built for cloud assistants, desktop tools connect in 1 step:

Add to your `claude_desktop_config.json` or Cursor MCP configuration:
```json
{
  "mcpServers": {
    "blender": {
      "url": "https://YOUR-TUNNEL-URL.trycloudflare.com/sse",
      "headers": {
        "Authorization": "Bearer YOUR_AGENT_API_KEY"
      }
    }
  }
}
```

---

### 4. Custom GPTs & ChatGPT Actions
* Paste your OpenAPI spec directly: `https://YOUR-TUNNEL-URL.trycloudflare.com/openapi.json`
* Choose **Authentication: Bearer** and provide your Agent API Key.

---

## 🚀 Quick Setup (60 Seconds)

### Option A: Install Inside Blender (Recommended)

1. Download **`mcp-cloud.zip`** from [Latest Releases](https://github.com/FahimXits/blender-mcp-cloud-bridge/releases).
2. In Blender, go to **Edit > Preferences > Get Extensions / Add-ons > Install from Disk...** and select `mcp-cloud.zip`.
3. Press `N` in the 3D Viewport to open the sidebar and click the **MCP** tab.
4. Select your tunnel provider:
   * **Cloudflare (Quick Tunnel):** No account or registration needed.
   * **ngrok (Recommended for Datacenters):** Prevents cloud IP blocking when agents call from AWS/GCP.
5. Click **Start Cloud Bridge** ➔ Click **Copy Agent Config** to pass directly to your AI agent!

---

### Option B: Standalone Companion CLI

If your Blender socket is already running on `127.0.0.1:9876`:

```bash
# Clone the repository
git clone https://github.com/FahimXits/blender-mcp-cloud-bridge.git
cd blender-mcp-cloud-bridge

# Run with Cloudflare Quick Tunnel
python bridge.py --tunnel cloudflare

# Or run with ngrok
python bridge.py --tunnel ngrok
```

---

## 🔒 Security

* **Guarded Execution:** All inbound connections require a secret `Bearer <KEY>` token.
* **Non-Blocking Architecture:** Never freezes Blender's viewport or UI thread while executing complex cloud requests.
* **Safe Termination:** Restricts destructive terminal operations (`sys.exit`) within the calling context.

---

## 📄 License

Distributed under the **GPL-3.0 License**. See [LICENSE](LICENSE) for details.
