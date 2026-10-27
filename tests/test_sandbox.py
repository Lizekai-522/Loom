import base64
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import unittest
from unittest.mock import patch

from loom.sandbox import (
    DockerSandbox, ENTRYPOINT, FrozenTest, PROXY_NAMES, RUNNER_SHA256, RUNNER_SOURCE,
    SandboxConfig, SandboxRejected, SandboxUnavailable, _Captured, _bundle, _capture,
)


IMAGE = "sha256:" + "a" * 64


def image_info():
    return {"Id": IMAGE, "Os": "linux", "Config": {
        "User": "10001:10001", "Entrypoint": ENTRYPOINT, "Volumes": None,
        "Labels": {"org.loom.runner.version": "loom-json-v1", "org.loom.runner.sha256": RUNNER_SHA256}}}


def inspection(config, name="owned"):
    return {"Image": IMAGE, "Config": {
        "Labels": {"org.loom.sandbox.owner": name}, "User": "10001:10001",
        "Entrypoint": ENTRYPOINT[:1], "Cmd": ENTRYPOINT[1:], "WorkingDir": "/workspace",
        "Env": [key + "=" for key in PROXY_NAMES]},
        "Mounts": [], "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
        "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "none",
            "IpcMode": "none", "PidMode": "", "CgroupnsMode": "private", "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": config.pids_limit,
            "Memory": config.memory_bytes, "MemorySwap": config.memory_bytes,
            "NanoCpus": int(config.cpus * 1_000_000_000), "Init": True,
            "LogConfig": {"Type": "none"}, "Tmpfs": {key:
                f"rw,nosuid,nodev,noexec,size={config.tmpfs_bytes},mode=700,uid=10001,gid=10001"
                for key in ("/workspace", "/tmp")}}}


class SandboxOfflineTests(unittest.TestCase):
    def setUp(self):
        self.config = SandboxConfig(IMAGE)
        self.sandbox = DockerSandbox(self.config)

    def test_only_immutable_explicit_images_are_accepted(self):
        for bad in ("python:latest", "loom:local", "sha256:abc", "--privileged", "", None):
            with self.subTest(bad=bad), self.assertRaises(SandboxRejected):
                SandboxConfig(bad)
        SandboxConfig("registry.example/loom@sha256:" + "b" * 64)

    def test_limits_reject_disabled_and_nonfinite_values(self):
        for key, value in (("pids_limit", -1), ("memory_bytes", 0), ("cpus", float("nan")),
                           ("timeout_seconds", float("inf")), ("max_files", True),
                           ("max_tests", 0), ("output_limit_bytes", 1.5)):
            with self.subTest(key=key), self.assertRaises(SandboxRejected):
                replace(self.config, **{key: value})

    def test_paths_and_nontext_sources_are_rejected_before_docker(self):
        for name in ("../x.py", "/x.py", "C:/x.py", "a\\x.py", "a/./x.py", "a//x.py",
                     "a/../x.py", "-x.py", "a\n.py", "secrets.pem", "x.py/../main.py"):
            with self.subTest(name=name), self.assertRaises(SandboxRejected):
                _bundle({name: "pass"}, name, self.config)
        with self.assertRaises(SandboxRejected):
            _bundle({"main.py": Path("/symlink")}, "main.py", self.config)
        with self.assertRaises(SandboxRejected):
            _bundle({"main.py": "pass", "main.py/x.py": "pass"}, "main.py", self.config)

    def test_file_size_count_and_entrypoint_limits(self):
        for files, entry, config in (({"main.py": "1234"}, "main.py", replace(self.config, max_file_bytes=3)),
                                     ({"main.py": "12", "x.py": "34"}, "main.py", replace(self.config, max_source_bytes=3)),
                                     ({"main.py": "", "x.py": ""}, "main.py", replace(self.config, max_files=1)),
                                     ({"x.py": "pass"}, "main.py", self.config)):
            with self.subTest(files=files), self.assertRaises(SandboxRejected):
                _bundle(files, entry, config)

    def test_archive_contains_only_deterministic_regular_source_files(self):
        files = {"main.py": "print(1)", "pkg/helper.py": "x=1"}
        archive, digest = _bundle(files, "main.py", self.config)
        other, same = _bundle(dict(reversed(list(files.items()))), "main.py", self.config)
        self.assertEqual((archive, digest), (other, same))
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            self.assertEqual(bundle.getnames(), ["main.py", "pkg/helper.py"])
            self.assertTrue(all(member.isfile() and member.mode == 0o400 for member in bundle))
        self.assertNotEqual(digest, _bundle({"main.py": "print(2)"}, "main.py", self.config)[1])

    def test_frozen_tests_are_required_and_finite_before_preflight(self):
        for tests in ([], [FrozenTest("same", {}, 1), FrozenTest("same", {}, 2)],
                      [FrozenTest("valid", {}, float("nan"))], [FrozenTest("x", {"x": object()}, 1)],
                      [FrozenTest("x", {1: "coerced-key"}, 1)], [FrozenTest("x", (1, 2), 1)],
                      [FrozenTest("../bad", {}, 1)]):
            with self.subTest(tests=tests), patch.object(self.sandbox, "preflight") as preflight:
                with self.assertRaises(SandboxRejected):
                    self.sandbox.run_python({"main.py": "pass"}, tests)
                preflight.assert_not_called()

    def test_preflight_fails_closed_without_daemon(self):
        with patch("loom.sandbox._capture", return_value=_Captured(1, b"", b"not available")):
            with self.assertRaises(SandboxUnavailable):
                self.sandbox.preflight()

    def test_preflight_checks_linux_resource_enforcement_and_runner_build(self):
        good = {"OSType": "linux", "MemoryLimit": True, "SwapLimit": True, "CpuCfsQuota": True,
                "PidsLimit": True, "SecurityOptions": ["name=seccomp,profile=builtin"], "ServerVersion": "29"}
        with patch.object(self.sandbox, "_command", side_effect=[json.dumps(good).encode(), json.dumps(image_info()).encode()]):
            self.assertEqual(self.sandbox.preflight()["image_id"], IMAGE)
        for field, value in (("OSType", "windows"), ("MemoryLimit", False), ("SwapLimit", False),
                             ("CpuCfsQuota", False), ("PidsLimit", False), ("SecurityOptions", [])):
            with self.subTest(field=field), patch.object(self.sandbox, "_command", return_value=json.dumps({**good, field: value}).encode()):
                with self.assertRaises(SandboxUnavailable):
                    self.sandbox.preflight()
        for key, value in (("User", "0"), ("Entrypoint", ["sh"]), ("Volumes", {"/data": {}}),
                           ("Labels", {"org.loom.runner.sha256": "fake"})):
            image = image_info()
            image["Config"][key] = value
            with self.subTest(key=key), patch.object(self.sandbox, "_command", side_effect=[json.dumps(good).encode(), json.dumps(image).encode()]):
                with self.assertRaises(SandboxRejected):
                    self.sandbox.preflight()

    def test_create_policy_has_no_host_mounts_shell_keys_or_model_commands(self):
        args = self.sandbox._create_args("owned", IMAGE)
        for flag in ("--read-only", "--init", "--cap-drop", "--pids-limit", "--memory", "--memory-swap",
                     "--cpus", "--security-opt", "--tmpfs"):
            self.assertIn(flag, args)
        for flag in ("--mount", "--volume", "--privileged", "--env-file", "--use-api-socket", "--publish"):
            self.assertNotIn(flag, args)
        self.assertEqual(args[-5:], [IMAGE, *ENTRYPOINT[1:]])
        self.assertNotIn("sh", args)

    def test_inspect_verifies_isolation_before_any_candidate_starts(self):
        self.sandbox._verify_policy(inspection(self.config), "owned", IMAGE)
        for key, value in (("ReadonlyRootfs", False), ("Privileged", True), ("NetworkMode", "bridge"),
                           ("PidMode", "host"), ("CapDrop", []), ("SecurityOpt", []), ("Memory", 0),
                           ("NanoCpus", 0), ("PidsLimit", -1), ("Binds", ["/:/host"]),
                           ("Tmpfs", {}), ("LogConfig", {"Type": "json-file"})):
            data = inspection(self.config)
            data["HostConfig"][key] = value
            with self.subTest(key=key), self.assertRaises(SandboxRejected):
                self.sandbox._verify_policy(data, "owned", IMAGE)
        data = inspection(self.config)
        data["Config"]["Env"] = [key + "=secret" for key in PROXY_NAMES]
        with self.assertRaises(SandboxRejected):
            self.sandbox._verify_policy(data, "owned", IMAGE)

    def _run_case(self, stdout=b"42\n", *, stderr=b"", code=0, attach_code=0, timeout=False, exceeded=False):
        captured = _Captured(attach_code, stdout, stderr, timeout, exceeded)
        states = [{"State": {"Running": False, "ExitCode": code, "OOMKilled": False}}]
        sent = []
        def command(*args):
            sent.append(args)
            return ("c" * 64).encode() if args[0] == "create" else b""
        def capture(argv, **kwargs):
            sent.append(("payload", kwargs["incoming"]))
            return captured
        with patch.object(self.sandbox, "_command", side_effect=command), patch.object(self.sandbox, "_verify_policy"), \
             patch.object(self.sandbox, "_inspect", side_effect=[{}, *states]), patch("loom.sandbox._capture", side_effect=capture):
            result = self.sandbox._case("case", b'{"x":21}', b"42", b"archive", "main.py", IMAGE)
        return result, sent

    def test_only_host_expected_json_and_zero_exit_can_pass(self):
        self.assertTrue(self._run_case()[0].passed)
        for output, status in ((b'{"tests.pass":true}', "failed"), (b"43", "failed"),
                               (b"42\n42", "protocol_error"), (b'NaN', "protocol_error"),
                               (b'{"x":1,"x":2}', "protocol_error"), (b"true", "failed"),
                               (b"42.0", "failed")):
            with self.subTest(output=output):
                result, _ = self._run_case(output)
                self.assertFalse(result.passed)
                self.assertEqual(result.status, status)
        self.assertEqual(self._run_case(code=1)[0].status, "runtime_error")
        with self.assertRaises(SandboxUnavailable):
            self._run_case(attach_code=1)

    def test_expected_tests_never_enter_container_and_cleanup_is_owned(self):
        result, sent = self._run_case()
        payload = next(item[1] for item in sent if item[0] == "payload")
        envelope = json.loads(payload)
        self.assertEqual(set(envelope), {"archive", "entrypoint", "input", "protocol"})
        self.assertEqual(base64.b64decode(envelope["input"]), b'{"x":21}\n')
        self.assertNotIn(b'expected', payload)
        create = next(item for item in sent if item[0] == "create")
        name = create[create.index("--name") + 1]
        self.assertEqual(sent[-1], ("container", "rm", "--force", name))

    def test_timeout_and_output_limits_cannot_pass_even_with_matching_output(self):
        self.assertEqual(self._run_case(timeout=True)[0].status, "timeout")
        self.assertEqual(self._run_case(exceeded=True)[0].status, "output_limit")

    def test_kill_race_requires_terminal_state_and_still_cleans_up(self):
        def command(*args):
            if args[:2] == ("container", "kill"):
                raise SandboxUnavailable("already exited")
            return ("c" * 64).encode()
        with patch.object(self.sandbox, "_command", side_effect=command), \
             patch.object(self.sandbox, "_verify_policy"), \
             patch.object(self.sandbox, "_inspect", side_effect=[{}, {"State": {"Running": False, "ExitCode": 0}}]), \
             patch("loom.sandbox._capture", return_value=_Captured(0, b"42", b"", False, True)):
            result = self.sandbox._case("case", b"0", b"42", b"archive", "main.py", IMAGE)
        self.assertEqual(result.status, "output_limit")

    def test_cleanup_runs_after_container_policy_failure(self):
        with patch.object(self.sandbox, "_command", return_value=("c" * 64).encode()) as command, \
             patch.object(self.sandbox, "_inspect", return_value={}), \
             patch.object(self.sandbox, "_verify_policy", side_effect=SandboxRejected("bad")):
            with self.assertRaises(SandboxRejected):
                self.sandbox._case("case", b"0", b"0", b"archive", "main.py", IMAGE)
            self.assertEqual(command.call_args.args[:3], ("container", "rm", "--force"))

    def test_capture_has_a_real_bound_on_retained_output_and_wall_time(self):
        # These are fixed, trusted transport fixtures, never candidate source.
        flood = _capture([sys.executable, "-I", "-c", "import os;\nwhile True: os.write(1,b'x'*4096)"], timeout=3, limit=1024)
        self.assertTrue(flood.output_exceeded)
        self.assertLessEqual(len(flood.stdout) + len(flood.stderr), 1024)
        timeout = _capture([sys.executable, "-I", "-c", "import time;time.sleep(3)"], timeout=0.1, limit=1024)
        self.assertTrue(timeout.timed_out)

    def test_dockerfile_embeds_the_same_reviewed_runner_and_pinned_base(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
        embedded = dockerfile.split("COPY <<'LOOM_RUNNER' /opt/loom/runner.py\n", 1)[1].split("LOOM_RUNNER\n", 1)[0]
        self.assertEqual(embedded, RUNNER_SOURCE)
        self.assertEqual(hashlib.sha256(embedded.encode()).hexdigest(), RUNNER_SHA256)
        self.assertIn("@sha256:", dockerfile.splitlines()[1])
        self.assertNotIn("COPY .", dockerfile)


if __name__ == "__main__":
    unittest.main()
