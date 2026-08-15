# Speaker-embedding (diarizer) service for Cloud Run.
# Base bundles CUDA-enabled torch 2.7.0 + torchaudio 2.7.0 we add pyannote
# + a tiny FastAPI server exposing only /v2/embedding (the endpoint the Omi backend calls).
FROM dustynv/pytorch:2.7-r36.4.0

# libsndfile/ffmpeg back the torchaudio/soundfile decoders used by pyannote.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY constraints.txt requirements.txt /app/
# The base image pins PIP_INDEX_URL (no pyannote there);
# force regular PyPI for these deps. torch/torchaudio stay put via constraints.txt.
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -c constraints.txt -r requirements.txt

COPY embedding.py main.py /app/

# HF model cache (mount a volume here at run time to persist the model across restarts).
ENV HF_HOME=/app/.hf
ENV MIN_EMBEDDING_AUDIO_DURATION=0.5
# Runs on CPU. (NVML INTERNAL ASSERT and intermittent
# for reliability (~1-2s per clip; embeddings are computed asynchronously). Revisit GPU if a
ENV CUDA_VISIBLE_DEVICES=""

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
