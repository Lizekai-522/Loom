import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import Request, urlopen


class CliTests(unittest.TestCase):
    def test_installed_module_starts_loopback_api_with_tenant_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            token = secrets.token_urlsafe(24)
            environment = dict(os.environ, LOOM_TENANT_KEYS=json.dumps({"smoke": token}))
            proc = subprocess.Popen([sys.executable, "-m", "loom", "serve", "--port", str(port), "--db", str(Path(directory) / "smoke.db")],
                                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                url = f"http://127.0.0.1:{port}"
                started = False
                for _ in range(150):
                    if proc.poll() is not None:
                        self.fail("API process exited during startup")
                    try:
                        with urlopen(url + "/health", timeout=0.2) as response:
                            started = json.load(response)["status"] == "ok"
                        break
                    except (URLError, TimeoutError):
                        time.sleep(0.05)
                self.assertTrue(started, "API did not become healthy within startup window")
                request = Request(url + "/v1/runs", data=json.dumps({"acceptance": ["tests.pass"]}).encode(),
                                  headers={"Authorization": "Bearer " + token, "Idempotency-Key": "smoke", "Content-Type": "application/json"})
                with urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 201)
                    self.assertEqual(json.load(response)["status"], "open")
            finally:
                proc.terminate()
                proc.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
