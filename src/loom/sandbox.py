"""Fail-closed, local Docker executor for Python JSON-in/JSON-out programs.

The host owns frozen test expectations. Container stdout is *candidate output*,
never a trusted assertion/coverage report. No candidate executes on the host.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import math
from pathlib import PurePosixPath
import re
import subprocess
import tarfile
import threading
import time
from typing import Any, Mapping, Sequence
from uuid import uuid4


RUNNER_VERSION = "loom-json-v1"
# This is the exact file embedded in sandbox/Dockerfile, including final LF.
RUNNER_SOURCE = '''import base64
import io
import json
import os
from pathlib import Path, PurePosixPath
import resource
import sys
import tarfile

def main():
    raw = sys.stdin.buffer.read(3000001)
    if len(raw) > 3000000:
        raise ValueError("envelope too large")
    data = json.loads(raw)
    if data.get("protocol") != "loom-json-v1":
        raise ValueError("protocol")
    archive = base64.b64decode(data["archive"], validate=True)
    if len(archive) > 1500000:
        raise ValueError("archive too large")
    root = Path("/workspace/src")
    root.mkdir(mode=0o700)
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle:
            path = PurePosixPath(member.name)
            if (not member.isfile() or member.name in seen or
                path.is_absolute() or any(p in ("", ".", "..") for p in member.name.split("/")) or
                "\\\\" in member.name or not member.name.endswith(".py") or
                len(member.name) > 200 or member.size > 262144):
                raise ValueError("unsafe member")
            seen.add(member.name)
            total += member.size
            if len(seen) > 64 or total > 1048576:
                raise ValueError("archive limits")
            target = root.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as out:
                out.write(bundle.extractfile(member).read())
            target.chmod(0o400)
    if data["entrypoint"] not in seen:
        raise ValueError("missing entrypoint")
    case_path = Path("/workspace/.input")
    case_path.write_bytes(base64.b64decode(data["input"], validate=True))
    incoming = os.open(case_path, os.O_RDONLY)
    case_path.unlink()
    os.dup2(incoming, 0)
    os.close(incoming)
    os.chdir(root)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "HOME": "/workspace", "TMPDIR": "/tmp", "PYTHONHASHSEED": "0"}
    bootstrap = 'import runpy,sys;sys.path.insert(0,"/workspace/src");runpy.run_path(sys.argv[1],run_name="__main__")'
    os.execve("/usr/local/bin/python", ["python", "-I", "-S", "-B", "-c", bootstrap,
              str(root / data["entrypoint"])], env)

try:
    main()
except Exception:
    print("sandbox runner rejected input", file=sys.stderr)
    sys.exit(120)
'''
RUNNER_SHA256 = hashlib.sha256(RUNNER_SOURCE.encode()).hexdigest()
ENTRYPOINT = ["/usr/local/bin/python", "-I", "-S", "-B", "/opt/loom/runner.py"]
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy", "FTP_PROXY", "ftp_proxy")
_PIN = re.compile(r"(?:sha256:[0-9a-f]{64}|[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64})\Z")
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")


class SandboxError(RuntimeError):
    """Infrastructure or policy failure; never substitute host execution."""


class SandboxUnavailable(SandboxError):
    pass


class SandboxRejected(SandboxError):
    pass


def _json(value: Any) -> bytes:
    def validate(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("strict JSON types required")
    try:
        validate(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SandboxRejected("value must be finite, UTF-8 JSON") from exc


def _load_json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("nonfinite JSON")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


@dataclass(frozen=True)
class SandboxConfig:
    image: str
    timeout_seconds: float = 5.0
    memory_bytes: int = 128 * 1024 * 1024
    cpus: float = 0.5
    pids_limit: int = 32
    tmpfs_bytes: int = 16 * 1024 * 1024
    output_limit_bytes: int = 64 * 1024
    max_files: int = 64
    max_file_bytes: int = 256 * 1024
    max_source_bytes: int = 1024 * 1024
    max_input_bytes: int = 16 * 1024
    max_tests: int = 32
    docker_binary: str = "docker"

    def __post_init__(self):
        if not isinstance(self.image, str) or not _PIN.fullmatch(self.image):
            raise SandboxRejected("image must be an explicitly approved immutable digest or image ID")
        ranges = {
            "timeout_seconds": (0.1, 120), "memory_bytes": (32 * 1024 * 1024, 2 * 1024**3),
            "cpus": (0.1, 2), "pids_limit": (8, 128), "tmpfs_bytes": (2 * 1024**2, 128 * 1024**2),
            "output_limit_bytes": (128, 1024**2), "max_files": (1, 64),
            "max_file_bytes": (1, 262144), "max_source_bytes": (1, 1048576),
            "max_input_bytes": (1, 65536), "max_tests": (1, 32),
        }
        for name, (low, high) in ranges.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise SandboxRejected(f"invalid {name}")
            if name not in ("timeout_seconds", "cpus") and not isinstance(value, int):
                raise SandboxRejected(f"{name} must be an integer")
        if not isinstance(self.docker_binary, str) or not self.docker_binary:
            raise SandboxRejected("docker binary is required")


@dataclass(frozen=True)
class FrozenTest:
    name: str
    input: Any
    expected: Any


@dataclass(frozen=True)
class CaseResult:
    name: str
    passed: bool
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_sha256: str
    duration_seconds: float
    oom_killed: bool = False


@dataclass(frozen=True)
class SandboxResult:
    passed: bool
    image_id: str
    source_sha256: str
    tests_sha256: str
    runner_sha256: str
    cases: tuple[CaseResult, ...]
    verdict_source: str = "host-frozen-json-v1"
    scope: str = "finite-black-box-tests; not arbitrary pytest/coverage or a correctness proof"

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class _Captured:
    code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False


def _capture(argv: Sequence[str], *, incoming: bytes = b"", timeout: float = 10,
             limit: int = 1024 * 1024) -> _Captured:
    """Drain both pipes continuously, retaining at most `limit` bytes in total."""
    try:
        process = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False)
    except OSError as exc:
        raise SandboxUnavailable("Docker CLI could not be started") from exc
    lock = threading.Lock()
    exceeded = threading.Event()
    buffers = [bytearray(), bytearray()]
    retained = 0
    def read(stream, index):
        nonlocal retained
        try:
            while chunk := stream.read(4096):
                with lock:
                    room = max(0, limit - retained)
                    buffers[index].extend(chunk[:room])
                    retained += min(room, len(chunk))
                    if len(chunk) > room:
                        exceeded.set()
        finally:
            stream.close()
    def write():
        try:
            process.stdin.write(incoming)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
    threads = [threading.Thread(target=read, args=(process.stdout, 0), daemon=True),
               threading.Thread(target=read, args=(process.stderr, 1), daemon=True),
               threading.Thread(target=write, daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None:
        if exceeded.is_set() or time.monotonic() >= deadline:
            timed_out = not exceeded.is_set()
            process.kill()  # Only this CLI process; caller removes its exact owned container.
            break
        time.sleep(0.01)
    process.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=2)
    return _Captured(process.returncode, bytes(buffers[0]), bytes(buffers[1]), timed_out, exceeded.is_set())


def _path(name: str) -> str:
    if (not isinstance(name, str) or len(name) > 200 or "\\" in name or
        PurePosixPath(name).is_absolute() or not name.endswith(".py") or
        any(not _PART.fullmatch(part) or part in (".", "..") for part in name.split("/"))):
        raise SandboxRejected("workspace requires safe relative POSIX .py paths")
    return name


def _bundle(files: Mapping[str, str], entrypoint: str, config: SandboxConfig) -> tuple[bytes, str]:
    _path(entrypoint)
    if not isinstance(files, Mapping) or not 1 <= len(files) <= config.max_files:
        raise SandboxRejected("invalid workspace file count")
    normalized = {}
    total = 0
    for name, source in files.items():
        _path(name)
        if not isinstance(source, str):
            raise SandboxRejected("source must be text; paths, streams and symlinks are not accepted")
        try:
            content = source.encode("utf-8")
        except UnicodeError as exc:
            raise SandboxRejected("source must be UTF-8") from exc
        total += len(content)
        if len(content) > config.max_file_bytes or total > config.max_source_bytes:
            raise SandboxRejected("workspace source limit exceeded")
        normalized[name] = content
    if entrypoint not in normalized:
        raise SandboxRejected("entrypoint is missing")
    # Reject file/parent collisions before handing anything to Docker.
    for name in normalized:
        if any(str(parent) in normalized for parent in PurePosixPath(name).parents):
            raise SandboxRejected("workspace file/directory collision")
    buffer = io.BytesIO()
    try:
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, content in sorted(normalized.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(content), 0o400, 0
                archive.addfile(info, io.BytesIO(content))
    except ValueError as exc:
        raise SandboxRejected("workspace path cannot be represented safely in USTAR") from exc
    archive_bytes = buffer.getvalue()
    return archive_bytes, hashlib.sha256(_json({name: value.decode("utf-8") for name, value in sorted(normalized.items())})).hexdigest()


class DockerSandbox:
    def __init__(self, config: SandboxConfig):
        self.config = config

    def _command(self, *args: str) -> bytes:
        result = _capture([self.config.docker_binary, *args], timeout=15)
        if result.code or result.timed_out or result.output_exceeded:
            raise SandboxUnavailable("Docker command failed or exceeded its control-plane bounds: " + args[0])
        return result.stdout

    def preflight(self) -> dict[str, str]:
        """Verify daemon capabilities and locally available operator-approved image."""
        try:
            info = _load_json(self._command("info", "--format", "{{json .}}"))
            if info.get("OSType") != "linux":
                raise SandboxUnavailable("a Linux Docker daemon is required")
            for field in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
                if info.get(field) is not True:
                    raise SandboxUnavailable(f"Docker does not enforce {field}")
            if not any("name=seccomp" in item for item in info.get("SecurityOptions", [])):
                raise SandboxUnavailable("Docker seccomp support is required")
            image = _load_json(self._command("image", "inspect", self.config.image, "--format", "{{json .}}"))
            cfg = image["Config"]
            if (image.get("Os") != "linux" or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", "")) or
                cfg.get("User") != "10001:10001" or cfg.get("Entrypoint") != ENTRYPOINT or
                cfg.get("Volumes") or cfg.get("Labels", {}).get("org.loom.runner.sha256") != RUNNER_SHA256 or
                cfg.get("Labels", {}).get("org.loom.runner.version") != RUNNER_VERSION):
                raise SandboxRejected("image is not the approved immutable Loom runner build")
            if self.config.image.startswith("sha256:") and image["Id"] != self.config.image:
                raise SandboxRejected("image identity changed")
            return {"image_id": image["Id"], "runner_sha256": RUNNER_SHA256,
                    "docker_version": str(info.get("ServerVersion", "unknown")), "os": "linux"}
        except (ValueError, KeyError, TypeError) as exc:
            raise SandboxUnavailable("Docker returned an invalid preflight response") from exc

    def _create_args(self, name: str, image_id: str) -> list[str]:
        c = self.config
        args = ["create", "--name", name, "--label", "org.loom.sandbox.owner=" + name,
                "--pull", "never", "--interactive", "--init", "--read-only", "--user", "10001:10001",
                "--network", "none", "--ipc", "none", "--cgroupns", "private", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--pids-limit", str(c.pids_limit),
                "--memory", str(c.memory_bytes), "--memory-swap", str(c.memory_bytes), "--cpus", str(c.cpus),
                "--ulimit", "nofile=64:64", "--ulimit", "core=0:0", "--log-driver", "none",
                "--workdir", "/workspace", "--no-healthcheck"]
        for directory in ("/workspace", "/tmp"):
            args += ["--tmpfs", f"{directory}:rw,nosuid,nodev,noexec,size={c.tmpfs_bytes},mode=700,uid=10001,gid=10001"]
        # Docker client proxy defaults must not inject proxy credentials.
        for variable in PROXY_NAMES:
            args += ["--env", variable + "="]
        args += ["--entrypoint", ENTRYPOINT[0], image_id, *ENTRYPOINT[1:]]
        return args

    def _inspect(self, container: str) -> dict:
        try:
            return _load_json(self._command("container", "inspect", container, "--format", "{{json .}}"))
        except (ValueError, TypeError) as exc:
            raise SandboxUnavailable("invalid Docker container inspection") from exc

    def _verify_policy(self, inspection: dict, name: str, image_id: str):
        c = self.config
        h, cfg = inspection.get("HostConfig", {}), inspection.get("Config", {})
        conditions = [
            inspection.get("Image") == image_id,
            cfg.get("Labels", {}).get("org.loom.sandbox.owner") == name,
            h.get("ReadonlyRootfs") is True, h.get("Privileged") is False,
            h.get("NetworkMode") == "none", h.get("IpcMode") == "none",
            h.get("PidMode", "") == "", h.get("CgroupnsMode") == "private",
            h.get("CapDrop") == ["ALL"], not h.get("CapAdd"),
            "no-new-privileges:true" in h.get("SecurityOpt", []),
            h.get("PidsLimit") == c.pids_limit, h.get("Memory") == c.memory_bytes,
            h.get("MemorySwap") == c.memory_bytes, h.get("NanoCpus") == int(c.cpus * 1_000_000_000),
            h.get("Init") is True, h.get("LogConfig", {}).get("Type") == "none",
            not h.get("Binds"), not h.get("Mounts"), not h.get("Devices"), not h.get("DeviceRequests"),
            not inspection.get("Mounts"), set(h.get("Tmpfs", {})) == {"/workspace", "/tmp"},
            cfg.get("User") == "10001:10001", cfg.get("Entrypoint") == ENTRYPOINT[:1],
            cfg.get("Cmd") == ENTRYPOINT[1:], cfg.get("WorkingDir") == "/workspace",
        ]
        for options in h.get("Tmpfs", {}).values():
            options = set(options.split(","))
            conditions.append({"rw", "nosuid", "nodev", "noexec", "mode=700", "uid=10001", "gid=10001", f"size={c.tmpfs_bytes}"} <= options)
        env = dict(item.split("=", 1) for item in cfg.get("Env", []) if "=" in item)
        conditions.append(all(env.get(variable) == "" for variable in PROXY_NAMES))
        if not all(conditions):
            raise SandboxRejected("Docker container did not retain the required isolation policy")

    def run_python(self, files: Mapping[str, str], tests: Sequence[FrozenTest], *, entrypoint: str = "main.py") -> SandboxResult:
        archive, source_hash = _bundle(files, entrypoint, self.config)
        if not isinstance(tests, Sequence) or not 1 <= len(tests) <= self.config.max_tests:
            raise SandboxRejected("at least one bounded platform-frozen test is required")
        frozen, names = [], set()
        for case in tests:
            if not isinstance(case, FrozenTest) or not isinstance(case.name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", case.name) or case.name in names:
                raise SandboxRejected("tests require unique safe names")
            names.add(case.name)
            incoming, expected = _json(case.input), _json(case.expected)
            if max(len(incoming), len(expected)) > self.config.max_input_bytes:
                raise SandboxRejected("test JSON exceeds the limit")
            frozen.append((case.name, incoming, expected))  # Snapshot before candidate starts.
        tests_hash = hashlib.sha256(_json([{ "name": n, "input": _load_json(i), "expected": _load_json(e)} for n, i, e in frozen])).hexdigest()
        ready = self.preflight()  # Never cache across runs; mutable tags are never accepted.
        cases = tuple(self._case(n, i, e, archive, entrypoint, ready["image_id"]) for n, i, e in frozen)
        return SandboxResult(all(case.passed for case in cases), ready["image_id"], source_hash,
                             tests_hash, RUNNER_SHA256, cases)

    def _case(self, name: str, incoming: bytes, expected: bytes, archive: bytes,
              entrypoint: str, image_id: str) -> CaseResult:
        container = "loom-sandbox-" + uuid4().hex
        started = time.monotonic()
        created = False
        try:
            raw_id = self._command(*self._create_args(container, image_id)).strip().decode("ascii")
            if not re.fullmatch(r"[0-9a-f]{64}", raw_id):
                raise SandboxUnavailable("invalid container ID")
            created = True
            self._verify_policy(self._inspect(container), container, image_id)
            envelope = _json({"protocol": RUNNER_VERSION, "archive": base64.b64encode(archive).decode("ascii"),
                              "entrypoint": entrypoint, "input": base64.b64encode(incoming + b"\n").decode("ascii")})
            output = _capture([self.config.docker_binary, "start", "--attach", "--interactive", container],
                              incoming=envelope, timeout=self.config.timeout_seconds,
                              limit=self.config.output_limit_bytes)
            if output.timed_out or output.output_exceeded:
                try:
                    self._command("container", "kill", container)
                except SandboxUnavailable:
                    # It may have exited between the bound being hit and kill.
                    # The following inspection must still prove it is stopped.
                    pass
            state = self._inspect(container).get("State", {})
            if state.get("Running") is not False or not isinstance(state.get("ExitCode"), int):
                raise SandboxUnavailable("container did not reach a verifiable terminal state")
            code = state["ExitCode"]
            status = "failed"
            if output.output_exceeded:
                status = "output_limit"
            elif output.timed_out:
                status = "timeout"
            elif state.get("OOMKilled"):
                status = "memory_limit"
            elif code != 0:
                status = "runtime_error"
            elif output.code != 0:
                raise SandboxUnavailable("Docker attach failed despite a zero container exit")
            else:
                try:
                    status = "passed" if _json(_load_json(output.stdout)) == expected else "failed"
                except (ValueError, UnicodeError, SandboxRejected, RecursionError):
                    status = "protocol_error"
            return CaseResult(name, status == "passed", status, code,
                              output.stdout.decode("utf-8", errors="replace"),
                              output.stderr.decode("utf-8", errors="replace"),
                              hashlib.sha256(output.stdout).hexdigest(), round(time.monotonic() - started, 4),
                              bool(state.get("OOMKilled")))
        finally:
            # Never enumerate/kill shared resources. Name is generated above, never candidate-controlled.
            if created:
                self._command("container", "rm", "--force", container)
