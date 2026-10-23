# Official Python tag index digest verified against Docker Hub on 2026-10-23.
FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
LABEL org.loom.runner.version="loom-json-v1" \
      org.loom.runner.sha256="ab3fd758ec426a61fe642f853a5872e67808805ef2e900718250ea1b12dc1e41"
COPY <<'LOOM_RUNNER' /opt/loom/runner.py
import base64
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
                "\\" in member.name or not member.name.endswith(".py") or
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
LOOM_RUNNER
RUN chmod 0555 /opt/loom /opt/loom/runner.py && mkdir -p /workspace && chmod 0700 /workspace && chown 10001:10001 /workspace
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/python", "-I", "-S", "-B", "/opt/loom/runner.py"]
