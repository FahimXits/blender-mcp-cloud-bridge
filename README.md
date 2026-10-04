# Blender MCP Cloud Bridge

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Blender](https://img.shields.io/badge/Blender-4.2%20%7C%205.x-E87D0D?style=flat-square&logo=blender&logoColor=white)](https://www.blender.org/)
[![Protocol](https://img.shields.io/badge/Protocol-MCP%20%2B%20REST-7928ca?style=flat-square)](https://modelcontextprotocol.io/)
[![Tunnel](https://img.shields.io/badge/Tunnel-Cloudflare%20%7C%20ngrok-F38020?style=flat-square)](https://www.cloudflare.com/)
[![License](https://img.shields.io/badge/License-GPL--3.0-green?style=flat-square)](LICENSE)

**A secure, zero-dependency bridge connecting cloud AI agents (Claude, Cursor, Muse AI, Hermes, ChatGPT) to local Blender 3D via the Model Context Protocol (MCP) and automated tunnels.**

</div>

---

## 💡 The Problem & The Solution

* **The Problem:** The official Blender MCP add-on runs exclusively on `localhost:9876`. Cloud-hosted AI agents (Claude, Cursor, custom cloud LLM pipelines, autonomous agents) cannot connect to local machines behind domestic routers and corporate firewalls.
* **The Solution:** **Blender MCP Cloud Bridge** provides an automated, authenticated HTTPS bridge with **zero external Python dependencies**. It provisions an instant public tunnel (Cloudflare or ngrok) and exposes standard **MCP (SSE + Streamable HTTP)** and **REST** endpoints guarded by Bearer API authentication.

---

## ⚡ Architecture

```
┌─────────────────────────────────┐
│     Cloud AI Agent / Client     │  (Claude Desktop, Cursor, Muse AI,
│   (MCP Client / OpenAPI / REST) │   Hermes, OpenClaw, Custom GPTs)
└────────────────┬────────────────┘
                 │  HTTPS + Bearer API Key
                 ▼
┌─────────────────────────────────┐
│  Secure Tunnel Edge Gateway     │  (Cloudflare Quick Tunnel or ngrok)
└────────────────┬────────────────┘
                 │  Loopback HTTP (Port 8765)
                 ▼
┌─────────────────────────────────┐
│    Blender MCP Cloud Bridge     │  (Zero-dependency Python runtime)
│   • Auth & Rate Limiting        │  • MCP JSON-RPC 2.0 (SSE / Messages)
│   • OpenAPI Spec Generator      │  • REST Endpoints (/execute, /health)
└────────────────┬────────────────┘
                 │  Null-byte JSON Socket (Port 9876)
                 ▼
┌─────────────────────────────────┐
│       Local Blender 3D          │  (Executes Python code with `bpy`
│       Active Workspace          │   context & sandboxed error guards)
└─────────────────────────────────┘
```

---

## 🚀 Quick Start (60 Seconds)

### Option 1: In-Blender Extension (Recommended)

1. Download **`mcp-cloud.zip`** from [Releases](https://github.com/FahimXits/blender-mcp-cloud-bridge/releases).
2. In Blender, navigate to **Edit > Preferences > Get Extensions / Add-ons > Install from Disk...** and select `mcp-cloud.zip`.
3. Open the **3D Viewport**, press `N` to open the sidebar, and select the **MCP** tab.
4. Select your preferred tunnel (**Cloudflare** or **ngrok**), configure your API key, and click **Start Cloud Bridge**.
5. Click **Copy Agent Config** and paste directly into your assistant!

---

### Option 2: Standalone Bridge Script

If your Blender MCP socket server is already listening on `localhost:9876`:

```bash
# Clone the repository
git clone https://github.com/FahimXits/blender-mcp-cloud-bridge.git
cd blender-mcp-cloud-bridge

# Run with Cloudflare Quick Tunnel (zero account needed)
python bridge.py --tunnel cloudflare

# Or run with ngrok
python bridge.py --tunnel ngrok
```

---

## 🔌 Connecting AI Assistants

### 1. Claude Desktop & Cursor (MCP SSE)

Add the following to your `claude_desktop_config.json` or Cursor MCP settings:

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

#### Available MCP Tools:
* `execute_blender_code`: Executes arbitrary Python code with direct access to the `bpy` module.
* `get_blender_scene_info`: Retrieves active scene metadata, object coordinates, materials, collections, and timeline states.

---

### 2. Custom GPTs & ChatGPT Actions

1. In ChatGPT GPT Builder, navigate to **Actions > Create new action**.
2. Set **Schema URL** to:
   ```
   https://YOUR-TUNNEL-URL.trycloudflare.com/openapi.json
   ```
3. Set **Authentication**:
   * Type: `API Key`
   * Auth Type: `Bearer`
   * Key: `YOUR_AGENT_API_KEY`

---

### 3. REST API (`POST /execute`)

Direct programmatic execution for autonomous cloud agents (Muse AI, Hermes, OpenClaw, LangChain):

```bash
curl -X POST "https://YOUR-TUNNEL-URL.trycloudflare.com/execute" \
  -H "Authorization: Bearer YOUR_AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "code": "import bpy\nbpy.ops.mesh.primitive_cube_add(location=(0,0,2))\nresult={\"name\": bpy.context.active_object.name}"
  }'
```

**Response Format:**
```json
{
  "status": "ok",
  "result": {
    "name": "Cube"
  },
  "stdout": "",
  "stderr": ""
}
```

---

## 🔒 Security & Best Practices

* **Bearer Token Authentication:** Every inbound request to `/execute`, `/sse`, `/messages`, and `/mcp` requires authentication matching `bridge_config.json`.
* **Private Configuration:** Never commit `bridge_config.json` with active keys. Use `bridge_config.example.json` as a template.
* **Code Sandboxing:** Code execution is governed by Blender's runtime sandbox, preventing fatal shell termination commands (`sys.exit`).

---

## 📄 License

Distributed under the **GPL-3.0 License**. See [LICENSE](LICENSE) for details.
