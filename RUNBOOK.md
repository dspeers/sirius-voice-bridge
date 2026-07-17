# Runbook — live voice control ("Hey Jarvis, sit")

Fully self-hosted voice commands for the Sirius dog. No cloud: local wake word (openWakeWord)
→ local speech-to-text (Whisper) → local LLM brain (Qwen via the `sirius-llm` repo) → dog acts.

## ✅ Proven working (2026-07-16)
"Hey Jarvis, sit down" → dog performed a sit skill. End to end, on-Mac, over the robot's
Volcano-ASR pipeline which we intercept on the LAN.

## One-time / after-Mac-reboot
1. **LLM brain up:** `~/sirius-llm/mac/start.command` (Ollama + `qwen2.5:7b-instruct` on `:11434`).
2. **Shim up:** `./run.sh` (or `.venv/bin/python shim.py`). Loads Whisper `small.en` + `hey_jarvis`,
   listens `wss://0.0.0.0:8443`.

## After the ROBOT reboots (important)
The robot's redirect is partly non-persistent:
- `/etc/hosts` (`192.168.4.252 openspeech.bytedance.com`) **survives** reboot.
- The self-signed cert trust **survives**.
- The **iptables DNAT does NOT** — re-apply it (root SSH to the robot):
  ```
  iptables -t nat -A OUTPUT -p tcp -d 192.168.4.252 --dport 443 \
      -j DNAT --to-destination 192.168.4.252:8443
  ```
Verify: `openssl s_client -connect openspeech.bytedance.com:443` from the robot shows
`CN=openspeech.bytedance.com` (our cert) and `Verify return code: 0`.

Also confirm the robot's AI still points at our brain:
`curl -s http://192.168.4.134:8088/api/v1/ai/credentials/status` → llm.base_url = `…4.252:11434/v1`.

## To give a command
1. **Tap "AI Talk" on the dog's face screen.** This starts the listening session — WITHOUT it,
   nothing is listening (this silently tanked every early test). LED goes to KEEP_AI/green.
2. Say **"Hey Jarvis, <command>"** — e.g. "Hey Jarvis, sit down" / "come here" / "roll over".
   Say the wake word and command in one breath; the pre-roll buffer keeps the command onset.
3. Watch `shim.log`: `WAKE 'hey_jarvis' (score …)` then `==> COMMAND '…'`. The dog's brain
   (Qwen) turns the text into a skill.

## Tuning (env vars, all optional)
| var | default | meaning |
|---|---|---|
| `WAKE` | 1 | 1 = wake-word front-end; 0 = fallback command-vocabulary gate |
| `WAKE_MODEL` | hey_jarvis | any openWakeWord stock model (alexa/hey_mycroft/hey_rhasspy) or a custom `.onnx` |
| `WAKE_THRESHOLD` | 0.5 | raise to reduce false wakes, lower if it misses the wake word |
| `CMD_PREROLL_MS` | 1000 | audio kept before the wake fires (fire is ~300-500ms late) |
| `CMD_WINDOW_MS` | 3500 | max command capture after wake |
| `WHISPER_MODEL` | small.en | base.en (faster) … medium.en (more accurate) |

## Changing the name
Swap `WAKE_MODEL` to another stock model, or train a custom openWakeWord model for any name
(offline, synthetic-TTS pipeline) and point `WAKE_MODEL` at the `.onnx`. Plumbing is name-agnostic.

## Troubleshooting
- **No `connect` in shim.log** → the AI session isn't started: tap AI Talk. After robot reboot,
  re-apply the DNAT (above).
- **`connect` but no `WAKE`** → wake word not heard: say "Hey Jarvis" clearly, closer to the dog;
  lower `WAKE_THRESHOLD` (e.g. 0.4).
- **`WAKE` but `false wake — no intelligible command`** → command too quiet/short; say a fuller
  phrase right after the wake word.
- **Dog replies in Chinese** → active character locale; see sirius-notes.md ("Locale gotcha").

## Known nice-to-haves (not built)
- **Hands-free** (no button): keep the session open by publishing `/ai_interaction/trigger {data:true}`
  (ROS2, `ROS_DOMAIN_ID=37`, CycloneDDS). Needs the robot's exact DDS context — parked.
- **TTS** (dog talks back): de-prioritized ("it's a dog").
