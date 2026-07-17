#!/usr/bin/env bash
# LLM-free direct voice control (mic → wake → Whisper → robot REST). See docs/direct-control-design.md.
# Self-contained: creates the venv + installs deps on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi
# ensure deps (idempotent)
./.venv/bin/python -c "import sounddevice, openwakeword, faster_whisper, webrtcvad, numpy, onnxruntime" 2>/dev/null \
  || ./.venv/bin/pip install -q sounddevice openwakeword onnxruntime faster-whisper webrtcvad-wheels numpy

# Needs the local Qwen for the LLM fallback (optional): ~/sirius-llm/mac/start.command
exec ./.venv/bin/python voice_direct.py "$@"
