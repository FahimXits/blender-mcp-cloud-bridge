import urllib.request
import urllib.error
import json
import time
import subprocess
import sys
from pathlib import Path

def run_tests():
    print("Starting bridge process in background...")
    script_dir = Path(__file__).resolve().parent
    proc = subprocess.Popen([sys.executable, str(script_dir / "bridge.py"), "--no-tunnel"], cwd=str(script_dir))
    time.sleep(2)

    try:
        # 1. Test /health
        print("\n[Test 1] Testing GET /health...")
        req = urllib.request.Request("http://127.0.0.1:8765/health")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            print("Response:", data)
            assert data["status"] == "online"
            print("Passed!")

        # 2. Read API key from config
        config_path = script_dir / "bridge_config.json"
        if not config_path.exists():
            config_path = script_dir / "bridge_config.example.json"
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            api_key = cfg.get("api_key", "test_key")

        # 3. Test /execute without auth (should fail with 401)
        print("\n[Test 2] Testing POST /execute without auth...")
        req = urllib.request.Request(
            "http://127.0.0.1:8765/execute",
            data=json.dumps({"code": "print('hello')"}).encode(),
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req) as resp:
                print("Unexpected success!")
                sys.exit(1)
        except urllib.error.HTTPError as e:
            print(f"Correctly received HTTP {e.code}: {e.read().decode()}")
            assert e.code == 401
            print("Passed!")

        # 4. Test /execute with auth (Blender not yet running, should return 400 with connection message)
        print("\n[Test 3] Testing POST /execute with auth...")
        req = urllib.request.Request(
            "http://127.0.0.1:8765/execute",
            data=json.dumps({"code": "print('hello')"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                print("Response:", data)
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            print("Received expected response from Blender error handler:", data)
            assert "Could not connect to Blender MCP server" in data["message"]
            print("Passed!")

        # 5. Test MCP initialize
        print("\n[Test 4] Testing POST /mcp (initialize)...")
        req = urllib.request.Request(
            "http://127.0.0.1:8765/mcp",
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"}
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            print("MCP Initialize Response:", data)
            assert data["result"]["serverInfo"]["name"] == "blender-mcp-cloud-bridge"
            print("Passed!")

        # 6. Test MCP tools/list
        print("\n[Test 5] Testing POST /mcp (tools/list)...")
        req = urllib.request.Request(
            "http://127.0.0.1:8765/mcp",
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {}
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            print("MCP Tools List Response:", data)
            tools = [t["name"] for t in data["result"]["tools"]]
            assert "execute_blender_code" in tools
            assert "get_blender_scene_info" in tools
            print("Passed!")

        print("\nALL BRIDGE TESTS PASSED!")

    finally:
        proc.terminate()
        proc.wait()

if __name__ == "__main__":
    run_tests()
