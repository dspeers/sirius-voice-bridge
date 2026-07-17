#!/usr/bin/env python3
"""Volcano SAUC ASR shim — impersonates wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
on the LAN and answers with LOCAL Whisper transcription, so the Sirius robot hears you without
any cloud. Protocol reverse-engineered in PROTOCOL.md.

Client frames (from robot): CLIENT_FULL config (JSON+gzip), CLIENT_AUDIO (raw+gzip PCM 16k/16bit/mono),
last audio has flags=0x2. No sequence numbers. We reply with a SERVER_FULL {result:{text,...}} frame.
"""
import asyncio, ssl, struct, gzip, json, os, sys, time
import numpy as np
import websockets
from faster_whisper import WhisperModel

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8443"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")

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
    header = bytes([0x11, 0x90, 0x11, 0x00])          # v1 / type=9 flags=0 / JSON+gzip / reserved
    return header + struct.pack(">I", len(payload)) + payload

def transcribe(pcm: bytes) -> str:
    if len(pcm) < 3200:                                # <~0.1s — nothing worth transcribing
        return ""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segs, _ = MODEL.transcribe(audio, language="en", beam_size=1, vad_filter=False)
    return " ".join(s.text for s in segs).strip()

async def handler(ws):
    print(f"{ts()} connect {ws.remote_address} {ws.request.path}", flush=True)
    pcm = bytearray()
    try:
        async for msg in ws:
            if not isinstance(msg, bytes) or len(msg) < 8:
                continue
            mtype, flags, ser, comp, payload = parse(msg)
            if mtype == 1:                              # config
                pcm = bytearray()
            elif mtype == 2:                            # audio chunk
                pcm += payload
                if flags & 0x2:                         # end-of-utterance → transcribe + reply
                    dur = len(pcm) / 2 / 16000
                    text = transcribe(bytes(pcm))
                    print(f"{ts()}   utterance {dur:.1f}s -> {text!r}", flush=True)
                    await ws.send(server_full(text))
                    pcm = bytearray()                   # reset for a possible next utterance
    except Exception as e:
        print(f"{ts()}   closed: {type(e).__name__}: {e}", flush=True)

async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(HERE, "cert.pem"), os.path.join(HERE, "key.pem"))
    print(f"{ts()} SAUC shim on wss://0.0.0.0:{PORT}  (Whisper={WHISPER_MODEL})", flush=True)
    async with websockets.serve(handler, "0.0.0.0", PORT, ssl=ctx, max_size=None):
        await asyncio.Future()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
