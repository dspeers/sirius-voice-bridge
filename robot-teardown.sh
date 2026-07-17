#!/usr/bin/env bash
# Undo robot-setup — restore normal Volcano routing. Run FROM the Mac.
# Usage: SSHPASS=<robot-root-pw> ./robot-teardown.sh <robot-ip> <shim-mac-ip> [port]
set -euo pipefail
ROBOT="${1:?robot ip}"; SHIM="${2:?shim mac ip}"; PORT="${3:-8443}"
: "${SSHPASS:?export SSHPASS}"
ssh_run(){ sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"$ROBOT" "$1" 2>&1 | grep -v 'Permanently added'; }
ssh_run "sed -i '/openspeech.bytedance.com/d' /etc/hosts"
ssh_run "iptables -t nat -D OUTPUT -p tcp -d $SHIM --dport 443 -j DNAT --to-destination $SHIM:$PORT 2>/dev/null || true"
ssh_run "rm -f /usr/local/share/ca-certificates/openspeech-shim.crt; update-ca-certificates --fresh 2>&1 | tail -1"
echo "reverted. (ASR creds left in place — harmless; the dead real server just won't answer.)"
