#!/usr/bin/env python3
"""Volcano SAUC ASR shim — impersonates wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
on the LAN and answers with LOCAL Whisper transcription, so the Sirius robot hears you without
any cloud. Protocol reverse-engineered in PROTOCOL.md.

The robot streams audio continuously and only sends an end-of-utterance (flags=0x2) at its 15s
hard cap. The real Volcano ASR does *server-side* voice-activity detection: it returns a phrase the
moment you stop talking. We replicate that here with webrtcvad — detect the silence gap after speech,
transcribe just that phrase, and send a `definite` result immediately. Without this the robot never
gets a prompt result, transcribes 15s of room noise, and Whisper hallucinates fluent nonsense.
"""
import asyncio, ssl, struct, gzip, json, os, re, time
import numpy as np
import webrtcvad
import websockets
from faster_whisper import WhisperModel

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8443"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small.en")

RATE = 16000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2          # 640B = 20ms of 16k/16bit mono
VAD_AGGR = int(os.environ.get("VAD_AGGR", "3"))    # 0..3 — higher = more aggressive noise rejection
SILENCE_END_MS = int(os.environ.get("SILENCE_MS", "600"))   # trailing silence that ends a phrase
MIN_SPEECH_MS = int(os.environ.get("MIN_SPEECH_MS", "250")) # ignore blips shorter than this
PREROLL_MS = 200                                   # keep a little audio before speech onset
ENERGY_THRESH = int(os.environ.get("ENERGY_THRESH", "600")) # RMS gate: only close, direct speech counts
MAX_PHRASE_MS = int(os.environ.get("MAX_PHRASE_MS", "6000"))# force-cut a phrase that runs this long

# Wake-word front-end (openWakeWord). When on, Whisper only runs on the ~2s AFTER the wake word
# fires — so a roomful of chatter never reaches the brain, and anyone can address the dog by name.
# "hey_jarvis" is a stock model (no training, no cloud) chosen to not collide with Siri/Alexa.
USE_WAKE = os.environ.get("WAKE", "1") != "0"
WAKE_MODEL_NAME = os.environ.get("WAKE_MODEL", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
CMD_WINDOW_MS = int(os.environ.get("CMD_WINDOW_MS", "3500"))   # max audio captured after a wake
CMD_LEAD_MS = int(os.environ.get("CMD_LEAD_MS", "1500"))       # grace for command to start after wake
CMD_PREROLL_MS = int(os.environ.get("CMD_PREROLL_MS", "1000")) # audio kept before the wake fires (fire is
                                                               # late, so the command onset lands here)
OWW_FRAME = 1280                                               # 80ms @16k — oww's native chunk size

def rms(frame: bytes) -> float:
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0

# Command gate — a bystander's chatter is dropped; only phrases that are either
# (a) addressed to "Sirius", or (b) a *terse* phrase containing a known dog command
# reach the brain. Terseness is what separates "Sit." from "we can sit and talk".
WAKE_WORDS = os.environ.get("WAKE_WORDS", "sirius,serius,cyrus")  # tight — loose variants false-trigger
GATE_ON = os.environ.get("GATE", "1") != "0"
MAX_CMD_WORDS = int(os.environ.get("MAX_CMD_WORDS", "3"))
_wake = [re.escape(w.strip().lower()) for w in WAKE_WORDS.split(",") if w.strip()]
WAKE_RE = re.compile(r"\b(?:" + "|".join(_wake) + r")\b[\s,.:;!?—-]*", re.I) if _wake else None
# Distinctive dog-command verbs only — deliberately excludes common conversational words
# (good, up, down, here, over, play, speak) that would false-trigger on bystander chatter.
# Multi-word commands ("come here", "roll over") are caught by their distinctive verb.
COMMAND_WORDS = set((
    "sit come stand lie lay shake paw stay wave dance spin roll bark "
    "fetch heel twist crouch bow beg jump"
).split())

def gate(text: str):
    """None → drop. Else the command text to forward to the robot's brain."""
    if not GATE_ON:
        return text
    if WAKE_RE is not None:                              # (a) addressed to the dog by name
        m = WAKE_RE.search(text)
        if m:
            cmd = text[m.end():].strip(" ,.?!—-").strip()
            return cmd or text                          # bare "Sirius" → pass phrase through
    words = re.findall(r"[a-z]+", text.lower())          # (b) terse + a known command word
    if 0 < len(words) <= MAX_CMD_WORDS and any(w in COMMAND_WORDS for w in words):
        return text
    return None

def ts(): return time.strftime("%H:%M:%S")
print(f"{ts()} loading Whisper '{WHISPER_MODEL}'…", flush=True)
MODEL = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print(f"{ts()} Whisper ready.", flush=True)

OWW = None
if USE_WAKE:
    print(f"{ts()} loading wake model '{WAKE_MODEL_NAME}'…", flush=True)
    from openwakeword.model import Model as OWWModel
    OWW = OWWModel(wakeword_models=[WAKE_MODEL_NAME], inference_framework="onnx")
    print(f"{ts()} wake model ready.", flush=True)

def parse(data: bytes):
    """Return (msg_type, flags, serialization, compression, payload_bytes)."""
    b1, b2 = data[1], data[2]
    mtype, flags = b1 >> 4, b1 & 0x0f
    ser, comp = b2 >> 4, b2 & 0x0f
    size = struct.unpack(">I", data[4:8])[0]
    payload = data[8:8 + size]
    if comp == 1:
        try: payload = gzip.decompress(payload)
        except Exception: pass
    return mtype, flags, ser, comp, payload

def server_full(text: str) -> bytes:
    """SERVER_FULL (type=9), JSON+gzip: {result:{text,utterances}}."""
    body = json.dumps({
        "result": {"text": text,
                   "utterances": [{"text": text, "start_time": 0, "end_time": 0, "definite": True}]}
    }).encode("utf-8")
    payload = gzip.compress(body)
    header = bytes([0x11, 0x90, 0x11, 0x00])       # v1 / type=9 flags=0 / JSON+gzip / reserved
    return header + struct.pack(">I", len(payload)) + payload

def transcribe(pcm: bytes) -> str:
    if len(pcm) < RATE * 2 * MIN_SPEECH_MS // 1000:
        return ""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    peak = float(np.abs(audio).max())                 # normalize quiet capture up (cap 8x) so
    if peak > 0:                                       # Whisper can resolve a soft/distant command
        audio = audio * min(8.0, 0.95 / peak)
    # No vad_filter here — webrtcvad already carved out the phrase; Whisper's internal
    # VAD would re-strip a short crisp command ("Sirius sit") as "no speech" → empty.
    segs, _ = MODEL.transcribe(audio, language="en", beam_size=1,
                               vad_filter=False, condition_on_previous_text=False)
    out = []
    for s in segs:
        if getattr(s, "no_speech_prob", 0.0) > 0.85:  # only drop segments that are almost surely silence
            continue
        out.append(s.text)
    text = " ".join(out).strip()
    # Whisper phantoms on short/noisy audio — drop so they don't masquerade as input.
    if re.sub(r"[^a-z ]", "", text.lower()).strip() in PHANTOMS:
        print(f"{ts()}   (phantom dropped: {text!r})", flush=True)
        return ""
    return text

PHANTOMS = {"thank you", "thanks for watching", "thanks for watching everyone",
            "you", "bye", "bye bye", "so", "okay", "thank you so much",
            "please subscribe", "thank you for watching"}

class Utt:
    """Per-connection VAD state — slices continuous audio into spoken phrases."""
    def __init__(self):
        self.vad = webrtcvad.Vad(VAD_AGGR)
        self.buf = bytearray()        # unframed leftover bytes
        self.speech = bytearray()     # current phrase PCM (incl. preroll + trailing silence)
        self.preroll = bytearray()    # rolling recent audio before speech starts
        self.in_speech = False
        self.silence_ms = 0
        self.had_speech = False

    def feed(self, payload: bytes):
        """Return list of finished-phrase PCM blobs detected in this chunk."""
        done = []
        self.buf += payload
        pre_max = RATE * 2 * PREROLL_MS // 1000
        while len(self.buf) >= FRAME_BYTES:
            frame = bytes(self.buf[:FRAME_BYTES]); del self.buf[:FRAME_BYTES]
            speech = False
            try: speech = self.vad.is_speech(frame, RATE) and rms(frame) >= ENERGY_THRESH
            except Exception: pass
            if speech:
                if not self.in_speech:
                    self.in_speech = True
                    self.speech = bytearray(self.preroll)   # start with preroll
                self.speech += frame
                self.silence_ms = 0
                self.had_speech = True
                if len(self.speech) >= RATE * 2 * MAX_PHRASE_MS // 1000:   # runaway phrase — force-cut
                    done.append(bytes(self.speech))
                    self.speech = bytearray(); self.in_speech = False; self.silence_ms = 0
            else:
                if self.in_speech:
                    self.speech += frame                    # keep trailing silence
                    self.silence_ms += FRAME_MS
                    if self.silence_ms >= SILENCE_END_MS:
                        done.append(bytes(self.speech))
                        self.speech = bytearray()
                        self.in_speech = False
                        self.silence_ms = 0
                else:
                    self.preroll += frame
                    if len(self.preroll) > pre_max:
                        del self.preroll[:len(self.preroll) - pre_max]
        return done

    def flush(self):
        """Robot forced end-of-utterance — emit whatever speech we have."""
        if self.in_speech and len(self.speech):
            blob = bytes(self.speech)
            self.speech = bytearray(); self.in_speech = False; self.silence_ms = 0
            return blob
        return None

class WakeConn:
    """Per-connection wake→command state machine. Runs openWakeWord continuously on the stream;
    once the wake word fires, captures the following speech (VAD-delimited) as the command."""
    def __init__(self):
        self.owwbuf = np.zeros(0, dtype=np.int16)   # samples pending an 80ms oww frame
        self.preroll = bytearray()                   # rolling recent audio while listening for wake
        self.capturing = False                       # False = listening for wake; True = grabbing command
        self.cmd = bytearray()                       # command audio captured after wake
        self.framebuf = bytearray()                  # bytes pending a 20ms VAD frame
        self.had_speech = False
        self.silence_ms = 0
        self.elapsed_ms = 0
        self.vad = webrtcvad.Vad(VAD_AGGR)
        if OWW is not None:
            OWW.reset()

    def feed_wake(self, payload: bytes) -> float:
        """Feed audio to the wake model; keep a rolling pre-roll; return peak score this chunk."""
        self.preroll += payload                      # pre-roll = last CMD_PREROLL_MS of audio
        pre_max = RATE * 2 * CMD_PREROLL_MS // 1000
        if len(self.preroll) > pre_max:
            del self.preroll[:len(self.preroll) - pre_max]
        self.owwbuf = np.concatenate([self.owwbuf, np.frombuffer(payload, dtype=np.int16)])
        peak = 0.0
        while len(self.owwbuf) >= OWW_FRAME:
            frame = self.owwbuf[:OWW_FRAME]; self.owwbuf = self.owwbuf[OWW_FRAME:]
            peak = max(peak, OWW.predict(frame).get(WAKE_MODEL_NAME, 0.0))
        return peak

    def start_capture(self):
        self.capturing = True
        self.cmd = bytearray(self.preroll)           # seed with pre-roll: late wake-fire won't clip onset
        self.framebuf = bytearray()
        self.had_speech = False; self.silence_ms = 0; self.elapsed_ms = 0
        self.owwbuf = np.zeros(0, dtype=np.int16)

    def feed_cmd(self, payload):
        """Accumulate post-wake audio; return finished command PCM when the phrase ends, else None."""
        self.cmd += payload
        self.framebuf += payload
        while len(self.framebuf) >= FRAME_BYTES:
            frame = bytes(self.framebuf[:FRAME_BYTES]); del self.framebuf[:FRAME_BYTES]
            self.elapsed_ms += FRAME_MS
            sp = False
            try: sp = self.vad.is_speech(frame, RATE) and rms(frame) >= ENERGY_THRESH
            except Exception: pass
            if sp:
                self.had_speech = True; self.silence_ms = 0
            elif self.had_speech:
                self.silence_ms += FRAME_MS
            done = (self.had_speech and self.silence_ms >= SILENCE_END_MS) \
                or self.elapsed_ms >= CMD_WINDOW_MS \
                or (not self.had_speech and self.elapsed_ms >= CMD_LEAD_MS)  # nobody spoke → false wake
            if done:
                return bytes(self.cmd)
        return None

async def handler_wake(ws, loop):
    """Wake-word front-end: 'Hey Jarvis' opens a command window; Whisper runs only on that."""
    c = WakeConn()
    async def finish():
        # Wake already confirmed intent — transcribe whatever we captured (pre-roll + command)
        # and decide by whether Whisper found words, not by our own VAD.
        text = await loop.run_in_executor(None, transcribe, bytes(c.cmd))
        if text:
            print(f"{ts()}   ==> COMMAND {text!r}", flush=True)
            await ws.send(server_full(text))
        else:
            print(f"{ts()}   (false wake — no intelligible command)", flush=True)
        c.capturing = False
        if OWW is not None: OWW.reset()
    async for msg in ws:
        if not isinstance(msg, bytes) or len(msg) < 8:
            continue
        mtype, flags, ser, comp, payload = parse(msg)
        if mtype == 1:                                      # config → new turn
            c = WakeConn()
        elif mtype == 2:
            if not c.capturing:
                score = c.feed_wake(payload)
                if score >= WAKE_THRESHOLD:
                    print(f"{ts()} WAKE '{WAKE_MODEL_NAME}' (score {score:.2f}) — listening for command", flush=True)
                    c.start_capture()
            else:
                done = c.feed_cmd(payload)
                if done is None and flags & 0x2:            # robot ended the turn mid-capture
                    done = bytes(c.cmd)
                if done is not None:
                    await finish()

async def handler(ws):
    print(f"{ts()} connect {ws.remote_address} {ws.request.path}", flush=True)
    loop = asyncio.get_event_loop()
    try:
        if USE_WAKE:
            await handler_wake(ws, loop)
        else:
            await handler_gate(ws, loop)
    except Exception as e:
        print(f"{ts()}   closed: {type(e).__name__}: {e}", flush=True)

async def handler_gate(ws, loop):
    """Fallback (WAKE=0): no wake word — transcribe every phrase, gate by command vocabulary."""
    u = Utt()
    async for msg in ws:
        if not isinstance(msg, bytes) or len(msg) < 8:
            continue
        mtype, flags, ser, comp, payload = parse(msg)
        if mtype == 1:                                  # config → new turn
            u = Utt()
        elif mtype == 2:                                # audio chunk
            phrases = u.feed(payload)
            if flags & 0x2:
                tail = u.flush()
                if tail: phrases.append(tail)
            for blob in phrases:
                dur = len(blob) / 2 / RATE
                text = await loop.run_in_executor(None, transcribe, blob)
                if not text:
                    print(f"{ts()}   phrase {dur:.1f}s -> (silence, skipped)", flush=True)
                    continue
                cmd = gate(text)
                if cmd is None:
                    print(f"{ts()}   phrase {dur:.1f}s -> {text!r}  (not a command, dropped)", flush=True)
                    continue
                print(f"{ts()}   phrase {dur:.1f}s -> {text!r}  ==> COMMAND {cmd!r}", flush=True)
                await ws.send(server_full(cmd))

async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(HERE, "cert.pem"), os.path.join(HERE, "key.pem"))
    mode = f"wake='{WAKE_MODEL_NAME}'@{WAKE_THRESHOLD}" if USE_WAKE else "command-vocab gate"
    print(f"{ts()} SAUC shim on wss://0.0.0.0:{PORT}  (Whisper={WHISPER_MODEL}, {mode})", flush=True)
    async with websockets.serve(handler, "0.0.0.0", PORT, ssl=ctx, max_size=None):
        await asyncio.Future()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
