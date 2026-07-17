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
import asyncio, ssl, struct, gzip, json, os, time
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

def rms(frame: bytes) -> float:
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if len(a) else 0.0

def ts(): return time.strftime("%H:%M:%S")
print(f"{ts()} loading Whisper '{WHISPER_MODEL}'…", flush=True)
MODEL = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print(f"{ts()} Whisper ready.", flush=True)

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
    segs, _ = MODEL.transcribe(audio, language="en", beam_size=1,
                               vad_filter=True, condition_on_previous_text=False,
                               no_speech_threshold=0.5)
    out = []
    for s in segs:
        if getattr(s, "no_speech_prob", 0.0) > 0.6:   # drop segments Whisper itself thinks are silence
            continue
        out.append(s.text)
    return " ".join(out).strip()

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

async def handler(ws):
    print(f"{ts()} connect {ws.remote_address} {ws.request.path}", flush=True)
    u = Utt()
    loop = asyncio.get_event_loop()
    try:
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
                    print(f"{ts()}   phrase {dur:.1f}s -> {text!r}", flush=True)
                    await ws.send(server_full(text))
    except Exception as e:
        print(f"{ts()}   closed: {type(e).__name__}: {e}", flush=True)

async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(HERE, "cert.pem"), os.path.join(HERE, "key.pem"))
    print(f"{ts()} SAUC shim on wss://0.0.0.0:{PORT}  (Whisper={WHISPER_MODEL}, "
          f"VAD={VAD_AGGR}, silence={SILENCE_END_MS}ms)", flush=True)
    async with websockets.serve(handler, "0.0.0.0", PORT, ssl=ctx, max_size=None):
        await asyncio.Future()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
