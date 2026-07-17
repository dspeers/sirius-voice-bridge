# Design: LLM-free direct voice control (tiered, with LLM-as-NLU fallback)

Status: **proposed / not yet built** (2026-07-17). Supersedes the ASR-interception approach for
*behavior commands* (that approach still documented in RUNBOOK.md and stays valid for conversation).

## Motivation

The current pipeline (intercept the robot's cloud ASR → feed the robot's on-device Qwen brain → brain
picks a skill) has three problems we hit repeatedly:
1. **The brain wanders** — "stand up" sometimes returns a conversational fallback ("You're welcome~",
   `skill=head_dip`) instead of the action. The LLM is doing *both* understanding and execution, and drifts.
2. **Latency** — ~6 s of LLM inference for what should be instant.
3. **Depends on the robot's weak far-field mic** — quiet capture (~-40 to -50 dBFS at 4 ft) garbles commands.

For a fixed set of ~10-20 dog commands, the LLM is not needed: the robot exposes every action over plain
REST (we drove all of them from the control panel). A command is a lookup + one HTTP POST.

## Architecture

```
[good mic, Mac/USB/phone] → wake word (openWakeWord) → Whisper → command→action table → robot REST API
```

Runs entirely on the Mac (or any LAN box). **Throws away**, for commands: the LLM, the TLS/ASR
interception, the DDS problem, the injected touchscreen taps, the onset-timeout hack, AND the robot's mic.
This is also the **audio-injection** answer — the audio source is a good mic wherever the user stands, so
the robot's far-field mic is out of the loop.

Robot REST surface for actions (base `http://192.168.4.134:8088`):
- `/api/v1/action/list`, `/api/v1/action/play`, `/action/stop`, `/action/stop-all` (224 actions)
- `/api/v1/gait/move/{forward,backward,left,right,turn-left,turn-right}`, `/gait/mode`, `/gait/velocity`, `/gait/stop`
- `/api/v1/transform/{body,head,complete}`, posture
- (get exact action names from `/action/list` when the dog is on)

## Tiered flow

1. Wake word → Whisper → transcript.
2. **Table match?** → fire the REST action **instantly** (covers ~99%: sit/stand/come/lie down/shake…).
3. **No match** → play a **"thinking" gesture**, then ask the *local* Qwen (sirius-llm on the Mac) to map
   the utterance to our action vocabulary (constrained prompt, below).
4. Qwen returns action(s) → we execute via REST. Returns `[]` → a small "confused" gesture (optionally
   hand off to full conversational LLM).

### Thinking gesture (heard-you feedback)
On fallback, immediately `POST /api/v1/action/play` a ponder animation — the robot already has these
(`stand_default_ponder_brief` seen in logs; pick the variant for the current posture). Covers the ~6 s of
inference and gives honest feedback: instant table hits respond immediately; a ponder means "give me a sec."

### LLM-as-NLU, not LLM-as-executor (the key distinction)
Do NOT route fallback through the robot's built-in brain (that's the path that chats instead of acting).
Query our own Qwen with a constrained prompt and keep execution in OUR hands:
```
System: You control a robot dog. Valid actions: sit, stand, lie_down, come, shake,
spin, roll_over, wave, bark, stop. The user said: "{text}".
Reply with ONLY a JSON list of actions, e.g. ["sit"] or ["spin","bark"], or [] if not a command.
```
Qwen returns `["sit"]`; we parse and fire the REST calls. The LLM does the one thing it's good at (fuzzy
language → known action) and never touches execution, so it can't improvise. Bonus: unlocks compound /
creative behaviors the table can't ("do a trick" → `["spin","bark"]`).

### Hybrid, not either/or
Table-first for speed + determinism; constrained-LLM fallback for oddball phrasing ("park it", "take a
seat", "chase your tail twice"); full conversational LLM only for genuine chat (`[]` from the mapper).

## Command → action table (fill from /action/list when dog is on)
| spoken (+ synonyms) | mechanism |
|---|---|
| sit / sit down / take a seat | action/play sit_default_* (or posture) |
| stand / stand up / up | action/play stand_default_* |
| lie down / down / lay down | action/play lie_default_* |
| come / come here / heel | gait/move forward (+ face-track) |
| shake / paw / give paw | action/play (shake skill) |
| spin / turn around | action/play (spin) |
| roll over | action/play (roll) |
| stop / stay | gait/stop + action/stop |
(exact `*_default_*` names TBD from /action/list)

## Personality system — what commands bypass, and what keeps running
The robot has a real, evolving personality stack in `ai_interaction_node` + `emotion_manager`:
- **MBTI character** (active bucket was `siriusEnfp` = ENFP); `/api/v1/user/mbti`, `/api/v1/ai/character`.
- **Persistent dialogue memory** across sessions: `/userdata/ai/dialogue_history`, bucketed (key, locale)
  e.g. `siriusEnfp__en-US`; `history_cap` param; `memory=1`; `/api/v1/ai/character/clear-history`.
- **Emotion state** that evolves + drives spontaneous behavior: `emotion_manager`, `/api/v1/emotion/{state,
  history,satiety}`; logs show `情绪=calm` + emotion-driven idle actions ("liveliness"). `satiety` = a
  Tamagotchi-like need.
- **Physiology sim**: `enable_physiology`, `phys_tick_ms`, `[phys]` bodily-condition values, "M7 relieve".
- **Bonding ritual**: `/api/v1/user/ritual-status` / `ritual-complete`.

Implications for this design:
1. Emotion / physiology / autonomous "liveliness" run **independently of the LLM** — commanding via REST
   does NOT stop the dog from having its evolving mood + spontaneous behaviors. Good: the puppet still has a soul.
2. Conversational **memory only grows when you talk to the LLM**. Commands routed around it don't feed
   personality development → keep the LLM in the loop for *chat* (the hybrid) if long-term personality matters.

## Open items / to test when Sirius is on
- **Audio-injection test**: prove a Mac-side mic → wake → Whisper → REST action makes the dog act (no robot mic).
- `GET /api/v1/action/list` → pin exact action names; decide per-command endpoint (action/play vs gait vs transform).
- Pick a mic (any decent USB mic / conference mic near where the user stands).
- Confirm posture-transition safety when firing actions directly (control panel already did this fine).
- Optional: verify Bluetooth **audio-input** profile (HFP) — BT stack exists (`sirius_bt`, BLE, xbox pad) but
  audio-input unconfirmed; a BT mic as the robot's source is a maybe.
