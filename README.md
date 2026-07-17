# sirius-voice-bridge (PRIVATE)

Local ASR (and later TTS) for the Hengbot Sirius dog — impersonates Volcano/ByteDance's speech
endpoints on your LAN so the robot's native 2.4.8 AI pipeline hears you via **local Whisper**
instead of a Chinese cloud. Companion to [`sirius-llm`](https://github.com/dspeers/sirius-llm) (the brain).

**Status:** recon + protocol capture DONE (see [PROTOCOL.md](PROTOCOL.md)). TLS interception works
(no cert pinning); the full SAUC client wire format is captured. Next: the Whisper-backed response side.

## How it works
```
you speak → robot VAD → robot dials wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
          → /etc/hosts + iptables reroute to THIS shim → Whisper transcribes
          → SAUC result frame → robot's LLM (self-hosted Qwen) → action ("sit")
```

## Pieces
- `capture_server.py` — impersonates the ASR endpoint, logs/parses the robot's frames (recon tool; done its job).
- `gen-cert.sh` — self-signed cert for `openspeech.bytedance.com`.
- `robot-setup.sh <robot-ip> <shim-mac-ip> [port]` — dummy ASR creds + `/etc/hosts` + iptables 443→shim + trust cert. (`SSHPASS` = robot root pw.)
- `robot-teardown.sh` — revert.
- TODO: `shim.py` (Whisper-backed SAUC server) + `start.command`.
