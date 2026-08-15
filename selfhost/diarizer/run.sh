#!/usr/bin/env bash
# Build + (re)run the Cloud Run speaker-embedding service.
# Serves the Omi HOSTED_SPEAKER_EMBEDDING_API_URL contract: POST /v2/embedding + GET /health on :8090.
# Requires ./.env in this dir containing HUGGINGFACE_TOKEN (the HF account must have accepted the
# pyannote/wespeaker-voxceleb-resnet34-LM terms). Runs on CPU (Cloud Run runs on CPU).
set -euo pipefail
cd "$(dirname "$0")"

docker build -t omi-diarizer:cloudrun .
docker rm -f omi-diarizer 2>/dev/null || true
mkdir -p hfcache
docker run -d --name omi-diarizer --restart unless-stopped \
  -p 8090:8080 \
  --env-file ./.env \
  -v "$(pwd)/hfcache:/app/.hf" \
  omi-diarizer:cloudrun

echo "Started omi-diarizer on :8090. Test:  curl -s localhost:8090/health"
