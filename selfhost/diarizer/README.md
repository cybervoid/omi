# Cloud Run speaker-embedding (diarizer) service

Self-hosted replacement for Omi's `diarizer` `/v2/embedding` endpoint, running on Cloud Run
The GCE backend calls it via
`HOSTED_SPEAKER_EMBEDDING_API_URL` for speaker identification / named-speaker matching.

## What it does
`POST /v2/embedding` (multipart `file`, WAV) → JSON embedding list, using
**`pyannote/wespeaker-voxceleb-resnet34-LM`** (same model as upstream Omi `/v2/embedding`, so
embeddings are comparable). `GET /health` → `{"status":"healthy"}`. Listens on host **:8090**
(container 8080).

## Files
- `Dockerfile` — based on `dustynv/pytorch:2.7-r36.4.0` (torch 2.7 + torchaudio 2.7 for x86_64).
- `embedding.py` / `main.py` — slimmed v2-only port of `backend/diarizer/`.
- `requirements.txt` / `constraints.txt` — pyannote.audio 3.x (4.x needs torch 2.8); torch pinned to base.
- `run.sh` — build + run.
- `.env` — **not committed**; must contain `HUGGINGFACE_TOKEN` (HF account must have accepted the
  `pyannote/wespeaker-voxceleb-resnet34-LM` gated-model terms).

## Notable decisions / gotchas
- **pyannote.audio 3.x**, not 4.x: 4.0.3 hard-pins `torch==2.8.0`; the base is torch 2.7.0.
- **huggingface_hub < 1.0**: pyannote 3.x calls `hf_hub_download(use_auth_token=...)`, removed in hub 1.0.
- **`weights_only=False`** is forced in `embedding.py` (torch 2.6+ default `True` rejects the
  checkpoint's pickled globals; the model is the official gated repo, so full-pickle load is fine).
- **Runs on CPU** (`CUDA_VISIBLE_DEVICES=""`).
  (NVML INTERNAL ASSERT, intermittent cuBLAS `CUBLAS_STATUS_ALLOC_FAILED`). The model is small, so

## Deploy (Cloud Run)
```bash
# context lives in ~/omi-diarizer/ in .env holds HUGGINGFACE_TOKEN
cd ~/omi-diarizer && ./run.sh
curl -s localhost:8090/health
```

## How the GCE backend reaches it
