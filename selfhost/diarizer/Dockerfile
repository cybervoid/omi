# Speaker-embedding (diarizer) service for the Jetson Orin Nano (L4T r36.4 / JetPack 6).
# Base bundles CUDA-enabled torch 2.7.0 + torchaudio 2.7.0 for aarch64; we add pyannote
# + a tiny FastAPI server exposing only /v2/embedding (the endpoint the Omi backend calls).
FROM dustynv/pytorch:2.7-r36.4.0

# libsndfile/ffmpeg back the torchaudio/soundfile decoders used by pyannote.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY constraints.txt requirements.txt /app/
# The base image pins PIP_INDEX_URL to pypi.jetson-ai-lab.dev (no pyannote there);
# force regular PyPI for these deps. torch/torchaudio stay put via constraints.txt.
RUN pip install --no-cache-dir --index-url https://pypi.org/simple -c constraints.txt -r requirements.txt

COPY embedding.py main.py /app/

# HF model cache (mount a volume here at run time to persist the model across restarts).
ENV HF_HOME=/app/.hf
ENV MIN_EMBEDDING_AUDIO_DURATION=0.5
# GPU inference on this Jetson torch build is unstable (NVML INTERNAL ASSERT and intermittent
# cuBLAS CUBLAS_STATUS_ALLOC_FAILED on Tegra). The wespeaker model is small, so run on CPU
# for reliability (~1-2s per clip; embeddings are computed asynchronously). Revisit GPU if a
# future JetPack/torch build stabilizes the Tegra CUDA allocator.
ENV CUDA_VISIBLE_DEVICES=""

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
