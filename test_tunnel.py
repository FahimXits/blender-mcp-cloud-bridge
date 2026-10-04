import subprocess
import time
import sys
import json
import urllib.request
from pathlib import Path

def test_tunnel():
    script_dir = Path(__file__).resolve().parent
    proc = subprocess.Popen([sys.executable, str(script_dir / "bridge.py")], cwd=str(script_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    pub_url = None
    start = time.time()
    try:
        while time.time() - start < 30:
            line = proc.stdout.readline()
            if not line:
                break
            print("[BRIDGE OUT]", line.strip())
            if "PUBLIC CLOUD URL:" in line:
                pub_url = line.split("PUBLIC CLOUD URL:")[1].split("<<<")[0].strip()
                break

        print("\nDiscovered Public URL:", pub_url)
        assert pub_url and pub_url.startswith("https://")

        # Give Cloudflare edge DNS a moment to propagate
        time.sleep(3)

        # Query health endpoint over the public HTTPS URL
        print(f"Testing public endpoint: {pub_url}/health")
        req = urllib.request.Request(f"{pub_url}/health", headers={"User-Agent": "BlenderMCPTest/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            print("Public Response received:", data)
            assert data["status"] == "online"
            print("\nPUBLIC CLOUDFLARE TUNNEL TEST SUCCEEDED 100%!")

    finally:
        proc.terminate()
        proc.wait()

if __name__ == "__main__":
    test_tunnel()
