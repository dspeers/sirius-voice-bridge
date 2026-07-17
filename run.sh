#!/usr/bin/env bash
# Start the ASR shim (wake word "Hey Jarvis" → local Whisper → robot's local brain). No cloud.
# Self-contained: creates the venv + installs deps on first run. See RUNBOOK.md.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "first run: creating venv + installing deps…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q websockets faster-whisper numpy webrtcvad-wheels openwakeword onnxruntime
fi

if [ ! -f cert.pem ] || [ ! -f key.pem ]; then
  echo "generating self-signed cert (CN=openspeech.bytedance.com)…"
  ./gen-cert.sh
fi

exec ./.venv/bin/python shim.py "$@"
