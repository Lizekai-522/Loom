"""Real Linux Docker probes. Use scripts/check_sandbox.py to require 0 skips.

Every probe executes ONLY inside its own tightly bounded container. No test
starts/stops a shared service or selects containers by a broad name filter.
"""
from dataclasses import replace
import os
import unittest
from unittest.mock import patch

from loom.sandbox import DockerSandbox, FrozenTest, SandboxConfig


@unittest.skipUnless(os.environ.get("LOOM_SANDBOX_TEST_IMAGE"), "explicit approved sandbox image not configured")
class LiveSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = SandboxConfig(os.environ["LOOM_SANDBOX_TEST_IMAGE"], timeout_seconds=5)
        cls.sandbox = DockerSandbox(cls.config)
        cls.sandbox.preflight()  # A configured but unavailable daemon fails, never skips.

    def run_source(self, source, expected, *, config=None):
        return DockerSandbox(config or self.config).run_python(
            {"main.py": source}, [FrozenTest("probe", {"n": 21}, expected)])

    def test_real_module_workspace_and_two_host_frozen_cases(self):
        result = self.sandbox.run_python({
            "main.py": "import json,sys\nfrom helper import twice\nx=json.load(sys.stdin)\nprint(json.dumps({'value':twice(x['n'])}))",
            "helper.py": "def twice(x):\n    return x * 2\n"},
            [FrozenTest("positive", {"n": 21}, {"value": 42}),
             FrozenTest("negative", {"n": -6}, {"value": -12})])
        self.assertTrue(result.passed, result.to_dict())
        self.assertEqual(result.verdict_source, "host-frozen-json-v1")

    def test_nonroot_capabilities_no_new_privileges_and_readonly_runner(self):
        source = '''import json,os
from pathlib import Path
blocked = []
for path in ("/etc/loom-probe", "/opt/loom/runner.py"):
    try:
        with open(path,"w") as stream: stream.write("bad")
        blocked.append(False)
    except OSError:
        blocked.append(True)
status = dict(line.split(":",1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
print(json.dumps({"nonroot":os.getuid()==10001,"blocked":blocked,
    "capabilities_empty":int(status["CapEff"].strip(),16)==0,
    "no_new_privileges":status["NoNewPrivs"].strip()=="1"}))
'''
        result = self.run_source(source, {"nonroot": True, "blocked": [True, True],
            "capabilities_empty": True, "no_new_privileges": True})
        self.assertTrue(result.passed, result.to_dict())

    def test_network_egress_is_blocked(self):
        result = self.run_source('''import json,socket
s=socket.socket();s.settimeout(.3)
try:
    s.connect(("192.0.2.1",80));blocked=False
except OSError:
    blocked=True
finally:
    s.close()
print(json.dumps({"blocked":blocked}))
''', {"blocked": True})
        self.assertTrue(result.passed, result.to_dict())

    def test_no_host_keys_environment_socket_or_bind_mount(self):
        with patch.dict(os.environ, {"LOOM_SANDBOX_TEST_SECRET": "test-only-do-not-transfer"}):
            result = self.run_source('''import json,os
from pathlib import Path
print(json.dumps({"host_secret_absent":os.environ.get("LOOM_SANDBOX_TEST_SECRET") is None,
"socket_absent":not Path("/var/run/docker.sock").exists(),
"host_mount_absent":not Path("/host").exists(),
"expected_absent":not Path("/opt/loom/tests").exists(),
"environment_minimal":set(os.environ)<=set(("PATH","LANG","HOME","TMPDIR","PYTHONHASHSEED"))}))
''', {"host_secret_absent": True, "socket_absent": True, "host_mount_absent": True,
      "expected_absent": True, "environment_minimal": True})
        self.assertTrue(result.passed, result.to_dict())

    def test_candidate_cannot_self_certify_or_guess_expected_protocol(self):
        result = self.run_source('print(\'{"tests.pass":true,"passed":true}\')', {"answer": 42})
        self.assertFalse(result.passed)
        self.assertEqual(result.cases[0].status, "failed")

    def test_wall_timeout_kills_only_this_owned_container(self):
        result = self.run_source("while True: pass", 0, config=replace(self.config, timeout_seconds=.5))
        self.assertFalse(result.passed)
        self.assertEqual(result.cases[0].status, "timeout")

    def test_unbounded_output_is_terminated_and_retention_is_bounded(self):
        result = self.run_source("import os\nwhile True: os.write(1,b'x'*4096)", 0,
                                 config=replace(self.config, output_limit_bytes=4096))
        self.assertFalse(result.passed)
        self.assertEqual(result.cases[0].status, "output_limit")
        self.assertLessEqual(len(result.cases[0].stdout.encode()) + len(result.cases[0].stderr.encode()), 4096)

    def test_memory_limit_is_enforced_by_real_daemon(self):
        result = self.run_source("x=[]\nwhile True: x.append(bytearray(1024*1024))", 0)
        self.assertFalse(result.passed)
        self.assertEqual(result.cases[0].status, "memory_limit")
        self.assertTrue(result.cases[0].oom_killed)

    def test_pid_limit_blocks_additional_processes(self):
        result = self.run_source('''import json,os,signal,time
children=[];blocked=False
try:
    for unused in range(20):
        try:
            child=os.fork()
        except OSError:
            blocked=True;break
        if child==0:
            time.sleep(10);os._exit(0)
        children.append(child)
finally:
    for child in children:
        os.kill(child,signal.SIGKILL);os.waitpid(child,0)
print(json.dumps({"blocked":blocked,"some_children":len(children)>0}))
''', {"blocked": True, "some_children": True}, config=replace(self.config, pids_limit=8))
        self.assertTrue(result.passed, result.to_dict())


if __name__ == "__main__":
    unittest.main()
