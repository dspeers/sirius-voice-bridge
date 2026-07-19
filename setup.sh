#!/usr/bin/env bash
# Provision the local voice brain on this machine: Ollama + Qwen model + Python venv/deps.
# Idempotent — safe to re-run. Homebrew is a PREREQUISITE (see README); this script does NOT
# install Homebrew — it errors out and tells you to install it first.
set -euo pipefail
cd "$(dirname "$0")"
echo "== Sirius voice setup =="

if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew not found."
  echo "  Install it first (one time), then re-run setup:"
  echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  echo "  (see README). Aborting."
  exit 1
fi
echo "[ok] Homebrew present"

if command -v ollama >/dev/null 2>&1; then
  echo "[ok] Ollama present"
else
  echo "[..] installing Ollama via Homebrew…"; brew install ollama
fi

if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "[..] starting Ollama…"
  brew services start ollama >/dev/null 2>&1 || (ollama serve >/tmp/ollama-serve.log 2>&1 &)
  for _ in $(seq 1 20); do curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
fi
curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && echo "[ok] Ollama running" || echo "[warn] Ollama not responding yet"

MODEL="${SIRIUS_MODEL:-qwen2.5:7b-instruct}"
if ollama list 2>/dev/null | grep -qi "qwen2.5"; then
  echo "[ok] model present ($MODEL)"
else
  echo "[..] pulling $MODEL (~4.7 GB — this can take several minutes)…"
  ollama pull "$MODEL"
fi

if [ ! -d .venv ]; then echo "[..] creating Python venv…"; python3 -m venv .venv; fi
echo "[..] installing Python deps…"
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q sounddevice openwakeword onnxruntime faster-whisper webrtcvad-wheels numpy
echo "[ok] Python deps installed"

# openWakeWord ships WITHOUT model files — they must be fetched, or Model() fails NO_SUCHFILE on first run.
echo "[..] downloading wake-word models…"
./.venv/bin/python -c "import openwakeword.utils as u; u.download_models()"
echo "[ok] wake-word models downloaded"

# Pre-fetch the Whisper model so the first run isn't slow and readiness shows green.
echo "[..] downloading Whisper model (${WHISPER_MODEL:-small.en})…"
./.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL:-small.en}', device='cpu', compute_type='int8')"
echo "[ok] Whisper model ready"

echo "== setup complete — recheck readiness in the panel =="
