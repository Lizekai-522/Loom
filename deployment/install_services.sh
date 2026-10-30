#!/usr/bin/env bash
# Install service definitions after a separate, successful private bootstrap.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then echo 'Run as root on the dedicated Linux host.' >&2; exit 1; fi
if [[ $# -ne 3 ]]; then echo 'Usage: install_services.sh SOURCE_ROOT STATE_ROOT HOSTNAME' >&2; exit 1; fi
loom_source=$(realpath -e -- "$1")
loom_state=$(realpath -e -- "$2")
loom_host=$3
if [[ ! $loom_source =~ ^/[A-Za-z0-9_./-]+$ || ! $loom_state =~ ^/[A-Za-z0-9_./-]+$ ||
      ! $loom_host =~ ^[a-z0-9]+([.-][a-z0-9]+)*$ || $loom_state == / || $loom_source == / ||
      $loom_state == "$loom_source" || $loom_state == "$loom_source/"* ]]; then
  echo 'Invalid deployment paths or host.' >&2; exit 1
fi
for loom_command in python3 docker systemctl caddy; do command -v "$loom_command" >/dev/null; done
test -f "$loom_source/scripts/serve_public_preview.py"
test -f "$loom_state/loom-public-preview/executor.json"
test -f "$loom_state/loom-public-preview/verification/config.json"
test ! -e /etc/systemd/system/loom-api.service
test ! -e /etc/systemd/system/loom-executor.service
test ! -e /etc/systemd/system/loom-verification.service
# Existing proxy configuration is intentionally never overwritten.
test ! -e /etc/caddy/Caddyfile.loom
getent group docker >/dev/null
if ! id loom >/dev/null 2>&1; then useradd --system --home-dir "$loom_state" --shell /usr/sbin/nologin loom; fi
python3 -m venv "$loom_source/.venv"
"$loom_source/.venv/bin/python" -m pip install --disable-pip-version-check 'pip>=26.2.2'
"$loom_source/.venv/bin/python" -m pip install --disable-pip-version-check "$loom_source"
"$loom_source/.venv/bin/python" -m pip check
"$loom_source/.venv/bin/python" - "$loom_state" "$loom_host" <<'PY'
import json,sys
from pathlib import Path
from loom.executor_config import load_executor_config,load_host_tests
from loom.execution import freeze_tests
from loom.verification_config import load_verification_config
from loom.sandbox import DockerSandbox,SandboxConfig
private=Path(sys.argv[1])/'loom-public-preview'
access=json.loads((private/'access.json').read_bytes())
assert access['origin']=='https://'+sys.argv[2] and access['port']==8799
config=load_executor_config(private/'executor.json')
for expected,path in config.host_tests.items():assert freeze_tests(load_host_tests(path))[1]==expected
verifier=load_verification_config(config.verification_config)
verifier.identities();verifier.load_signers()
DockerSandbox(SandboxConfig(config.sandbox_image)).preflight()
print('Private configuration and immutable Docker runner checked; no model request sent.')
PY
chown -R loom:loom -- "$loom_state"
chmod 0700 -- "$loom_state"
for loom_service in api executor verification; do
  case "$loom_service" in
    api) loom_args="scripts/serve_public_preview.py --work-root $loom_state"; loom_groups='';;
    executor) loom_args="scripts/run_local_executor.py --db $loom_state/loom-public-preview/preview.db --config $loom_state/loom-public-preview/executor.json --watch"; loom_groups='SupplementaryGroups=docker';;
    verification) loom_args="scripts/run_verification.py drain --db $loom_state/loom-public-preview/preview.db --config $loom_state/loom-public-preview/verification/config.json --watch"; loom_groups='';;
  esac
  cat >"/etc/systemd/system/loom-$loom_service.service" <<EOF
[Unit]
Description=Loom $loom_service
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=loom
Group=loom
$loom_groups
WorkingDirectory=$loom_source
ExecStart=$loom_source/.venv/bin/python -X utf8 $loom_source/$loom_args
Restart=on-failure
RestartSec=5
UMask=0077
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$loom_state
StandardOutput=journal
StandardError=journal
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
done
cat >/etc/caddy/Caddyfile.loom <<EOF
$loom_host {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8799
}
EOF
caddy validate --config /etc/caddy/Caddyfile.loom --adapter caddyfile
systemctl daemon-reload
# No automatic exposure: inspect proxy config, DNS and firewall before starting.
echo 'Services installed. Configure Caddy to import /etc/caddy/Caddyfile.loom, then start the three Loom units.'
echo 'Only the executor receives Docker group access. API stays on 127.0.0.1:8799.'
