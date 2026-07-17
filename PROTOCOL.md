# Volcano SAUC ASR — captured wire protocol (Sirius 2.4.8)

Empirically captured by impersonating `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async`
(via `/etc/hosts` + iptables 443→shim + self-signed cert — **TLS interception works, no pinning**).

## Connection
- `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` (client `WebSocket++/0.8.2`)
- Auth via headers: `X-Api-App-Key`, `X-Api-Access-Key` (both = our dummy creds — **shim ignores auth**),
  `X-Api-Resource-Id: volc.bigasr.sauc.duration`, `X-Api-Connect-Id: <uuid>`
- Robot opens a NEW session each time VAD fires; streams until end-of-utterance, then closes.

## Frame format
`[4-byte header][4-byte big-endian payload size][payload]`  — **no sequence numbers** (flags carry state).
Header bytes:
- b0 = `0x11` (protocol v1, header 4 bytes)
- b1 = `(message_type<<4) | flags`
- b2 = `(serialization<<4) | compression`  (serialization: 0=raw 1=JSON; compression: 1=gzip)
- b3 = `0x00`

## Client → server (what the robot sends)
| frame | b1 | b2 | payload |
|---|---|---|---|
| config (1st) | `0x10` type=1 | `0x11` JSON+gzip | `{"audio":{"format":"pcm","rate":16000,"bits":16,"channel":1,"codec":"raw","language":"en-US"},"request":{"model_name":"bigmodel","enable_itn":true,"enable_punc":true,"enable_nonstream":true,"show_utterances":true,"end_window_size":800},"user":{"uid":"sirius-agent"}}` |
| audio | `0x20` type=2 | `0x01` raw+gzip | gzip(PCM 16k/16bit/mono, ~20ms/640B chunks) |
| audio LAST | `0x22` type=2 flags=0x2 | `0x01` | final gzip PCM chunk — **flags=0x2 = end-of-utterance** |

## Server → client (what OUR shim must send back)
- message_type = 9 (SERVER_FULL), serialization=1 (JSON), compression=1 (gzip)
- payload JSON: robot reads **`text`** (+ `utterances` when `show_utterances`). Standard SAUC:
  `{"result":{"text":"sit","utterances":[{"text":"sit","start_time":0,"end_time":800,"definite":true}]}}`

## Shim TODO (repo status)
- [x] TLS interception + redirect proven; full client protocol captured (`capture_server.py`)
- [ ] Response side: parse config+audio (gunzip, accumulate to flags=0x2) → **Whisper** → send SERVER_FULL result
- [ ] `start.command` runner (venv + faster-whisper) on the Mac (M2 Max, Metal)
