#!/usr/bin/env bash
# Self-signed cert for openspeech.bytedance.com (the host we impersonate locally).
set -e
cd "$(dirname "$0")"
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 3650 \
  -subj "/CN=openspeech.bytedance.com" \
  -addext "subjectAltName=DNS:openspeech.bytedance.com,DNS:*.bytedance.com"
echo "wrote cert.pem + key.pem (CN=openspeech.bytedance.com)"
