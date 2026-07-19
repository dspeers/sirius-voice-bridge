# sirius-voice-bridge

Fully self-hosted, private voice control for the [Hengbot Sirius](https://github.com/dspeers/sirius-control-panel)
robot dog — **no cloud**. A good mic on your Mac → wake word → local speech-to-text → the dog does it.
Companion to [`sirius-llm`](https://github.com/dspeers/sirius-llm) (the local LLM brain) and
[`sirius-control-panel`](https://github.com/dspeers/sirius-control-panel) (the web UI, which can run and
monitor this for you).

## Two approaches (the first is the one to use)

**1. Direct control — `voice_direct.py` (recommended).**
```
[good mic] → wake word "Hey Jarvis" (openWakeWord) → Whisper → command table → robot REST action
```
No cloud, no LLM in the hot path, no robot mic. Common commands (sit/stand/lie/come/shake/spin/dance/
turn/…) map straight to the robot's REST action API — instant and deterministic. Anything off the table
falls back to the local Qwen, prompted to map your words onto the action vocabulary (it never chats
instead of acting). Design: [`docs/direct-control-design.md`](docs/direct-control-design.md).

**2. ASR interception — `shim.py` (older, for the robot's *native* AI pipeline).**
Impersonates ByteDance/Volcano's ASR endpoint on the LAN (TLS MITM via `/etc/hosts` + iptables + a
trusted cert) so the robot's own 2.4.8 AI hears you via local Whisper. Heavier (needs on-robot setup)
and uses the robot's weak far-field mic. See [`RUNBOOK.md`](RUNBOOK.md) and [`PROTOCOL.md`](PROTOCOL.md).

## Prerequisites (macOS)
- **macOS on Apple Silicon** (tested on an M2 Max).
- **Homebrew** — a one-time install (this project does *not* install it for you):
  ```
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
- **Python 3** (comes with the Homebrew toolchain / macOS).
- **A microphone.** A decent USB/condenser mic (e.g. a Blue Yeti) works far better than the robot's own mic —
  put it wherever you actually stand.
- **Internet** for the one-time setup: it downloads the Qwen model (~4.7 GB) and the Whisper/wake models.
- The robot reachable on your LAN (REST on `:8088`).

Everything below Homebrew is installed automatically by `setup.sh`.

## Setup (one time, or on a new machine)
```
./setup.sh
```
Idempotent. Installs Ollama, pulls `qwen2.5:7b-instruct`, creates the venv, installs the Python deps.
Errors out with instructions if Homebrew is missing. (Or click **“set up / install”** in the
control-panel’s Voice card, which runs this and streams the progress.)

## Run
```
./run-direct.command                 # mic → Hey Jarvis → dog
ROBOT=192.168.4.134:8088 ./run-direct.command
```
Or just hit **Start** on the control panel’s Voice card — it supervises this process and shows a live
feed of what the speech system is doing (wake / transcript / matched command).

Then say: **“Hey Jarvis, sit down”** (stand up, lie down, come here, shake, spin, dance, turn left/right,
forward, back, bark…). Off-menu phrasing (“do a trick”, “take a seat and give me your paw”) goes to the LLM.

## Config (env vars, all optional)
`ROBOT` · `WHISPER_MODEL` (small.en) · `WAKE_MODEL` (hey_jarvis) · `WAKE_THRESHOLD` · `LLM_MODEL`
(qwen2.5:7b-instruct) · `LLM_URL` · `MIC_DEVICE` · `DRIVE_SPEED` · `DRIVE_MS` — see the top of
`voice_direct.py`. It emits `voice_direct.events.jsonl` for the panel’s monitor.

## Related
- [`sirius-control-panel`](https://github.com/dspeers/sirius-control-panel) — web UI; runs + monitors this.
- [`sirius-llm`](https://github.com/dspeers/sirius-llm) — the local Qwen brain (Ollama).
