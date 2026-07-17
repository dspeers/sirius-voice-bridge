#!/usr/bin/env bash
# Route the robot's Volcano ASR to our local shim. Run FROM the Mac.
# Usage: SSHPASS=<robot-root-pw> ./robot-setup.sh <robot-ip> <shim-mac-ip> [port]
set -euo pipefail
ROBOT="${1:?robot ip}"; SHIM="${2:?shim mac ip}"; PORT="${3:-8443}"
: "${SSHPASS:?export SSHPASS with the robot root password}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ssh_run(){ sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"$ROBOT" "$1" 2>&1 | grep -v 'Permanently added'; }

echo "1) dummy ASR creds (so VolcanoAsr actually dials out)…"
curl -s -m8 -X POST "http://$ROBOT:8088/api/v1/ai/credentials" -H 'Content-Type: application/json' \
  -d '{"asr":{"app_id":"shim","access_key":"shim","resource_id":"volc.bigasr.sauc.duration"}}' >/dev/null && echo "   ok"

echo "2) /etc/hosts: openspeech.bytedance.com -> $SHIM…"
ssh_run "sed -i '/openspeech.bytedance.com/d' /etc/hosts; printf '%s openspeech.bytedance.com\n' '$SHIM' >> /etc/hosts; grep openspeech /etc/hosts"

echo "3) iptables: remap :443 -> $SHIM:$PORT (Mac shim runs unprivileged)…"
ssh_run "iptables -t nat -D OUTPUT -p tcp -d $SHIM --dport 443 -j DNAT --to-destination $SHIM:$PORT 2>/dev/null || true; iptables -t nat -A OUTPUT -p tcp -d $SHIM --dport 443 -j DNAT --to-destination $SHIM:$PORT; echo '   rule set'"

echo "4) trust our self-signed cert (in case it verifies)…"
sshpass -e scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$HERE/cert.pem" root@"$ROBOT":/usr/local/share/ca-certificates/openspeech-shim.crt 2>&1 | grep -v 'Permanently added' || true
ssh_run "update-ca-certificates 2>&1 | tail -1"

echo "done — trigger AI Talk (screen button / wake word / gamepad X) and watch the shim."
