#!/usr/bin/env python3
"""Capture server — impersonates Volcano's SAUC ASR WSS endpoint and LOGS whatever
the robot sends, so we learn the exact wire protocol empirically before building the
real shim. Parses the Volcengine binary framing (4-byte header + optional seq + payload).
"""
import asyncio, ssl, struct, gzip, json, datetime, sys, os
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8443"))
MSG_TYPES = {1:"CLIENT_FULL(config)", 2:"CLIENT_AUDIO", 9:"SERVER_FULL", 11:"SERVER_ACK", 15:"ERROR"}

def ts(): return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def parse_frame(data: bytes):
    if len(data) < 4:
        return {"raw_len": len(data), "note": "short frame", "hex": data.hex()}
    b0, b1, b2, b3 = data[0], data[1], data[2], data[3]
    ver = b0 >> 4; hdr_size = (b0 & 0x0f) * 4
    msg_type = b1 >> 4; flags = b1 & 0x0f
    ser = b2 >> 4; comp = b2 & 0x0f
    off = hdr_size
    seq = None
    if flags & 0x01 or flags & 0x02 or flags & 0x03:      # sequence present
        if off + 4 <= len(data):
            seq = struct.unpack(">i", data[off:off+4])[0]; off += 4
    psize = None; payload = None
    if off + 4 <= len(data):
        psize = struct.unpack(">I", data[off:off+4])[0]; off += 4
        payload = data[off:off+psize] if off + psize <= len(data) else data[off:]
    decoded = None
    if payload is not None:
        p = payload
        if comp == 1:
            try: p = gzip.decompress(p)
            except Exception as e: p = payload; decoded = f"<gzip-fail {e}>"
        if decoded is None:
            if ser == 1:
                try: decoded = json.loads(p.decode("utf-8"))
                except Exception: decoded = f"<{len(payload)}B non-json>"
            else:
                decoded = f"<raw/audio {len(payload)}B>"
    return {"ver": ver, "hdr": hdr_size, "type": MSG_TYPES.get(msg_type, msg_type),
            "flags": f"0x{flags:x}", "ser": ser, "comp": comp, "seq": seq,
            "psize": psize, "payload": decoded}

async def handler(ws):
    peer = ws.remote_address
    hdrs = {k: v for k, v in ws.request.headers.items()
            if k.lower().startswith("x-api") or k.lower() in ("host","authorization","user-agent")}
    print(f"\n{ts()} ===== CONNECT from {peer}  path={ws.request.path}")
    print(f"{ts()}   headers: {json.dumps(hdrs)}")
    raw = open(os.path.join(HERE, "capture.bin"), "ab")
    n = 0
    try:
        async for msg in ws:
            n += 1
            if isinstance(msg, bytes):
                raw.write(struct.pack(">I", len(msg))); raw.write(msg); raw.flush()
                info = parse_frame(msg)
                pl = info.get("payload")
                if isinstance(pl, (dict, list)): pl = json.dumps(pl, ensure_ascii=False)[:400]
                print(f"{ts()}   #{n} [{len(msg)}B] type={info.get('type')} flags={info.get('flags')} "
                      f"ser={info.get('ser')} comp={info.get('comp')} seq={info.get('seq')} -> {pl}")
            else:
                print(f"{ts()}   #{n} TEXT: {msg[:300]}")
    except Exception as e:
        print(f"{ts()}   closed: {type(e).__name__}: {e}")
    finally:
        raw.close()
        print(f"{ts()} ===== DISCONNECT ({n} frames)")

async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(HERE, "cert.pem"), os.path.join(HERE, "key.pem"))
    print(f"{ts()} SAUC capture server on wss://0.0.0.0:{PORT}  (impersonating openspeech.bytedance.com)")
    async with websockets.serve(handler, "0.0.0.0", PORT, ssl=ctx, max_size=None):
        await asyncio.Future()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
