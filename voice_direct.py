#!/usr/bin/env python3
"""voice_direct.py — LLM-free (mostly) voice control for the Hengbot Sirius dog.

    [good mic] → wake word "Hey Jarvis" → Whisper → command table → robot REST action

No cloud, no LLM in the hot path, no ASR interception, no DDS, no injected taps, and NOT the robot's
weak far-field mic — audio comes from a good mic on THIS machine, wherever you stand. On a table miss
we play a "thinking" gesture and ask the local Qwen (Ollama) to map the utterance to our action
vocabulary (LLM-as-NLU, not executor), then WE execute via REST. See docs/direct-control-design.md.

Deps (see run-direct.sh):  sounddevice  openwakeword  onnxruntime  faster-whisper  webrtcvad-wheels  numpy
The robot's action REST shapes are taken from the working control panel:
    GET  /api/v1/action/list?limit=1000   -> data.action_base_path, data.actions[{file|filename,...}]
    POST /api/v1/action/play  {file_path: <base>/<file.avi>, torque: <int>}
    GET  /api/v1/action/status            -> whether an action is still playing (for chaining)
"""
import json, os, re, sys, time, urllib.request, urllib.error
import numpy as np
import webrtcvad
import sounddevice as sd
from faster_whisper import WhisperModel

# ---- config (all env-overridable) -------------------------------------------------------------
ROBOT      = os.environ.get("ROBOT", "192.168.4.134:8088")
ROBOT_BASE = f"http://{ROBOT}/api/v1"
WHISPER_MODEL  = os.environ.get("WHISPER_MODEL", "small.en")  # Mac mic is clean; small.en is plenty
WAKE_MODEL     = os.environ.get("WAKE_MODEL", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
TORQUE     = int(os.environ.get("TORQUE", "2047"))            # 2047 = full; matches the panel's default
DRIVE_SPEED = float(os.environ.get("DRIVE_SPEED", "0.35"))   # gait speed 0.05-0.5 (higher = faster)
DRIVE_MS    = int(os.environ.get("DRIVE_MS", "1300"))        # how long a movement command drives, then stop
MIC_DEVICE = os.environ.get("MIC_DEVICE")                     # sounddevice device name/index, or None=default
LLM_ENABLE = os.environ.get("LLM_ENABLE", "1") != "0"
LLM_URL    = os.environ.get("LLM_URL", "http://localhost:11434/v1")   # local Ollama (sirius-llm)
LLM_MODEL  = os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct")

RATE, FRAME_MS = 16000, 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2       # 640B = 20ms
OWW_FRAME   = 1280                              # 80ms — oww native chunk
SILENCE_END_MS = int(os.environ.get("SILENCE_MS", "700"))
MAX_CMD_MS     = int(os.environ.get("MAX_CMD_MS", "4000"))
CMD_PREROLL_MS = int(os.environ.get("CMD_PREROLL_MS", "900"))  # audio kept before the wake fires (the
                                                               # wake triggers ~400ms late; keeps command onset)
VAD_AGGR       = int(os.environ.get("VAD_AGGR", "2"))
MAX_UTTER_WORDS = int(os.environ.get("MAX_UTTER_WORDS", "8"))  # real commands are terse; ignore long
                                                              # rambles (a bystander conversation that
                                                              # false-tripped the wake word)

def ts(): return time.strftime("%H:%M:%S")
def log(m): print(f"{ts()} {m}", flush=True)

# Structured event log (JSONL) for the web-UI monitor to tail; additive, alongside the human log.
HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_PATH = os.environ.get("EVENTS_PATH", os.path.join(HERE, "voice_direct.events.jsonl"))
_ev = open(EVENTS_PATH, "a", buffering=1)
def event(kind, **f): _ev.write(json.dumps({"t": time.time(), "kind": kind, **f}) + "\n")

# ---- command vocabulary ----------------------------------------------------------------------
# canonical command -> (keywords that trigger it, regex(es) to find the action .avi in /action/list).
# The action-file guesses below are resolved/verified against the LIVE action list at startup, so
# exact vendor names don't have to be hard-coded — we pick the best match from what the robot reports.
COMMANDS = {
    "sit":       (["sit", "sit down", "take a seat", "park it"],      [r"^sit_default.*(idle|brief)"]),
    "stand":     (["stand", "stand up", "get up", "stand back up"],   [r"^stand_default.*(idle|returnPosition)"]),
    "lie_down":  (["lie down", "lay down", "lie", "down", "lay"],     [r"^lie_default.*(idle|brief)"]),
    "shake":     (["shake", "paw", "give paw", "shake hands"],        [r"paw", r"shake"]),
    "spin":      (["spin", "spin around", "twirl", "turn around", "turnaround", "about face"], [r"spin"]),  # gait_*_spin_jump
    "dance":     (["dance", "do a dance", "boogie"],                  [r"stand_default_dance.*brief"]),
    "wave":      (["wave", "say hi", "say hello", "hello", "greet"],  [r"greet", r"hello", r"wave"]),
    "bark":      (["bark", "speak", "talk"],                          [r"bark"]),
    # movement LAST so a specific action verb ("spin") wins a tie over a bare direction ("left")
    "forward":   (["forward", "go forward", "walk", "come", "come here", "here", "heel"], None),  # gait
    "backward":  (["backward", "backwards", "back up", "back", "go back", "reverse"], None),        # gait
    "turn_left": (["turn left", "left"],                              None),                        # gait
    "turn_right":(["turn right", "right"],                            None),                        # gait
    "stop":      (["stop", "stay", "freeze", "halt"],                 None),   # stop movement/action
}
# words that let the LLM fallback know something is a command vocabulary (for the constrained prompt)
VOCAB = list(COMMANDS.keys())
THINK_PATTERNS = [r"ponder", r"confused", r"think"]   # a "thinking" gesture for the fallback

MOVE_CMDS = {"forward", "backward", "turn_left", "turn_right", "stop"}

def match_command(text: str):
    """Return a canonical command if the transcript clearly names one, else None.
    A specific action verb (spin/sit/dance/…) beats a bare direction (left/right) even if the
    direction word is longer — "spin to the right" is a spin, not a turn. Within the same
    category, the longer keyword wins ("sit down" over "down", "turn left" over "left")."""
    t = " " + re.sub(r"[^a-z ]", " ", text.lower()) + " "
    best = None   # (cmd, kw_len, is_move)
    for cmd, (kws, _) in COMMANDS.items():
        is_move = cmd in MOVE_CMDS
        for kw in kws:
            if f" {kw} " not in t:
                continue
            if best is None:
                best = (cmd, len(kw), is_move)
            elif best[2] and not is_move:                 # action beats move
                best = (cmd, len(kw), is_move)
            elif best[2] == is_move and len(kw) > best[1]:  # same category → longer wins
                best = (cmd, len(kw), is_move)
    return best[0] if best else None

# ---- robot REST client -----------------------------------------------------------------------
class Robot:
    def __init__(self):
        self.base_path = "/root/material/actions"
        self.actions = []                    # list of file basenames from /action/list
        self.resolved = {}                   # canonical command -> action file (or None for gait/stop)

    def _req(self, path, method="GET", body=None, timeout=6):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{ROBOT_BASE}/{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log(f"  ! REST {method} {path} failed: {e}")
            return None

    def load_actions(self):
        r = self._req("action/list?limit=1000")
        if not r or not r.get("data"):
            log("  ! could not load /action/list — is the dog on? using stub names")
            return
        d = r["data"]
        self.base_path = d.get("action_base_path", self.base_path)
        self.actions = [a.get("file") or a.get("filename") for a in d.get("actions", []) if (a.get("file") or a.get("filename"))]
        log(f"  loaded {len(self.actions)} actions, base={self.base_path}")

    def _find(self, patterns):
        """First action file matching any regex (case-insensitive), preferring shorter/'idle' names."""
        if not patterns:
            return None
        cands = [f for f in self.actions if any(re.search(p, f, re.I) for p in patterns)]
        cands.sort(key=lambda f: (0 if re.search(r"idle|brief", f, re.I) else 1, len(f)))
        return cands[0] if cands else None

    def resolve(self):
        """Map each command's action-file patterns to a concrete file from the live list."""
        for cmd, (_, pats) in COMMANDS.items():
            self.resolved[cmd] = self._find(pats)     # None for gait-based (come/stop) or unmatched
        self.think = self._find(THINK_PATTERNS)
        log(f"  resolved actions: " + ", ".join(f"{c}->{self.resolved[c]}" for c in VOCAB))
        if not self.think:
            log("  (no 'ponder' gesture found for thinking fallback)")

    def play(self, file, torque=TORQUE):
        if not file:
            return
        self._req("action/play", "POST", {"file_path": f"{self.base_path}/{file}", "torque": torque})

    def gait_stop(self):
        self._req("gait/stop", "POST", {})

    GAIT = {"forward": "gait/move/forward", "backward": "gait/move/backward",
            "turn_left": "gait/move/turn-left", "turn_right": "gait/move/turn-right"}

    def drive(self, endpoint):
        # the panel drives by re-POSTing {speed} every 350ms while held, then gait/stop
        for _ in range(max(1, DRIVE_MS // 350)):
            self._req(endpoint, "POST", {"speed": DRIVE_SPEED}); time.sleep(0.35)
        self.gait_stop()

    def perform(self, cmd):
        """Execute a canonical command via the right mechanism."""
        if cmd == "stop":     return self.gait_stop()
        if cmd in self.GAIT:  return self.drive(self.GAIT[cmd])
        self.play(self.resolved.get(cmd))

    def ponder(self):
        if self.think:
            self.play(self.think)

# ---- LLM-as-NLU fallback (constrained; we execute) -------------------------------------------
def llm_map(text: str):
    """Ask the local Qwen to map an utterance to our action vocabulary. Returns a list of canonical
    commands (subset of VOCAB), or [] if it's not a command. The LLM never executes anything."""
    sys_prompt = (
        "You control a robot dog. You translate a person's words into actions the dog can perform. "
        f"Valid actions (use these exact names): {', '.join(VOCAB)}. "
        'Reply with ONLY a JSON array of action names to perform in order, e.g. ["sit"] or '
        '["spin","bark"]. If the words are not a command for the dog, reply []. No other text.'
    )
    body = {"model": LLM_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": text}]}
    try:
        req = urllib.request.Request(f"{LLM_URL}/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            content = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  ! LLM query failed: {e}")
        return []
    m = re.search(r"\[.*\]", content, re.S)          # be forgiving about surrounding text
    if not m:
        return []
    try:
        acts = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [a for a in acts if a in VOCAB]

# ---- audio: wake word + command capture from the local mic -----------------------------------
def transcribe(model, pcm: bytes) -> str:
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segs, _ = model.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False)
    return " ".join(s.text for s in segs).strip()

def main():
    log(f"loading Whisper '{WHISPER_MODEL}'…")
    whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    log(f"loading wake model '{WAKE_MODEL}'…")
    from openwakeword.model import Model as OWWModel
    oww = OWWModel(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    vad = webrtcvad.Vad(VAD_AGGR)

    robot = Robot()
    robot.load_actions()
    robot.resolve()

    log(f"listening on mic (device={MIC_DEVICE or 'default'}) — say \"Hey {WAKE_MODEL.split('_')[-1].title()}, <command>\"")
    event("ready", whisper=WHISPER_MODEL, wake=WAKE_MODEL, robot=ROBOT, llm=LLM_ENABLE,
          mic=(MIC_DEVICE or "default"), actions={c: robot.resolved.get(c) for c in VOCAB})
    owwbuf = np.zeros(0, dtype=np.int16)
    preroll = bytearray(); pre_max = RATE * 2 * CMD_PREROLL_MS // 1000

    def read_block(stream, n):
        data, _ = stream.read(n)
        return np.frombuffer(bytes(data), dtype=np.int16)

    with sd.RawInputStream(samplerate=RATE, blocksize=OWW_FRAME, dtype="int16", channels=1,
                           device=MIC_DEVICE) as stream:
        while True:
            blk = read_block(stream, OWW_FRAME)
            preroll += blk.tobytes()                       # rolling pre-roll of recent audio
            if len(preroll) > pre_max:
                del preroll[:len(preroll) - pre_max]
            owwbuf = np.concatenate([owwbuf, blk])
            fired = False
            while len(owwbuf) >= OWW_FRAME:
                frame = owwbuf[:OWW_FRAME]; owwbuf = owwbuf[OWW_FRAME:]
                if oww.predict(frame).get(WAKE_MODEL, 0.0) >= WAKE_THRESHOLD:
                    fired = True; break
            if not fired:
                continue

            log(f"WAKE — capturing command…")
            event("wake")
            oww.reset(); owwbuf = np.zeros(0, dtype=np.int16)
            # seed with pre-roll so the late wake-fire doesn't clip the command onset ("sit"),
            # then keep capturing until ~SILENCE_END_MS of trailing silence or MAX_CMD_MS.
            pcm = bytearray(preroll); preroll = bytearray()
            had_speech = False; silence = 0; elapsed = 0
            leftover = bytearray()
            while elapsed < MAX_CMD_MS:
                block = read_block(stream, OWW_FRAME).tobytes()
                leftover += block
                while len(leftover) >= FRAME_BYTES:
                    f = bytes(leftover[:FRAME_BYTES]); del leftover[:FRAME_BYTES]
                    pcm += f; elapsed += FRAME_MS
                    sp = False
                    try: sp = vad.is_speech(f, RATE)
                    except Exception: pass
                    if sp: had_speech = True; silence = 0
                    elif had_speech: silence += FRAME_MS
                if had_speech and silence >= SILENCE_END_MS:
                    break
            text = transcribe(whisper, bytes(pcm))
            log(f"  heard: {text!r}")
            event("heard", text=text)
            if not text:
                continue
            if len(re.findall(r"[a-z]+", text.lower())) > MAX_UTTER_WORDS:
                log(f"  (ignored — too long, likely conversation not a command)")
                event("ignored", text=text, reason="too_long")
                continue

            # a conjunction implies multiple actions → let the LLM order+chain them;
            # otherwise take the instant table path.
            compound = bool(re.search(r"\b(and|then|also|after)\b", text.lower()))
            cmd = None if compound else match_command(text)
            if cmd:
                log(f"  → command '{cmd}' (table)")
                event("command", text=text, commands=[cmd], via="table")
                robot.perform(cmd)
                continue
            # fallback: thinking gesture + constrained LLM → actions (handles compounds)
            if not LLM_ENABLE:
                log("  (no table match; LLM fallback disabled)"); continue
            log("  → no table match; pondering + asking LLM…")
            robot.ponder()
            acts = llm_map(text)
            if acts:
                log(f"  ← LLM mapped to {acts}")
                event("command", text=text, commands=acts, via="llm")
                for a in acts:
                    robot.perform(a); time.sleep(0.4)
            else:
                log("  ← LLM: not a command")
                event("no_command", text=text)
                robot.play(robot._find([r"confused"]))   # small confused gesture if one exists

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: pass
