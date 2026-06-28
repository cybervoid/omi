"""Speaker embedding (v2 / wespeaker) for the Jetson-hosted diarizer.

Faithful port of the /v2/embedding path from backend/diarizer/embedding.py so the
embeddings are produced by the SAME model (pyannote/wespeaker-voxceleb-resnet34-LM)
and remain comparable with anything the backend already stored. v1 (pyannote/embedding)
and the diarization pipeline are intentionally dropped — the backend only calls /v2.
"""

import os
import shutil
import uuid
import wave

import torch
import torchaudio
from fastapi import HTTPException, UploadFile
from pyannote.audio import Model, Inference

# Minimum audio duration (seconds) for speaker embedding extraction.
# Audio shorter than this crashes wespeaker fbank (upstream issue #4572).
MIN_EMBEDDING_AUDIO_DURATION = float(os.getenv("MIN_EMBEDDING_AUDIO_DURATION", "0.5"))


def _get_audio_duration_from_file(file_path: str) -> float:
    """Duration in seconds; stdlib wave first (fast), then torchaudio fallback."""
    try:
        with wave.open(file_path, "rb") as wf:
            framerate = wf.getframerate()
            if framerate <= 0:
                return 0.0
            return wf.getnframes() / framerate
    except (wave.Error, EOFError, OSError):
        pass
    try:
        info = torchaudio.info(file_path)
        if info.sample_rate <= 0:
            return 0.0
        return info.num_frames / info.sample_rate
    except Exception:
        return 0.0


def _validate_audio_duration(file_path: str):
    duration = _get_audio_duration_from_file(file_path)
    if duration < MIN_EMBEDDING_AUDIO_DURATION:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "audio_too_short",
                "min_duration": MIN_EMBEDDING_AUDIO_DURATION,
                "actual_duration": round(duration, 3),
            },
        )


# pyannote 3.x loads its checkpoint via torch.load() without weights_only=False; torch 2.6+
# defaults weights_only=True and rejects pickled globals (e.g. torch_version.TorchVersion).
# The checkpoint is the official (gated) pyannote model fetched with our token, so loading
# the full pickle is acceptable here.
_torch_load_orig = torch.load


def _torch_load_full(*args, **kwargs):
    # Force-override: lightning passes weights_only=True explicitly, so setdefault is not enough.
    kwargs["weights_only"] = False
    return _torch_load_orig(*args, **kwargs)


torch.load = _torch_load_full

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
embedding_model_v2 = Model.from_pretrained(
    "pyannote/wespeaker-voxceleb-resnet34-LM", token=os.getenv("HUGGINGFACE_TOKEN")
)
embedding_inference_v2 = Inference(embedding_model_v2, window="whole")
embedding_inference_v2.to(device)

os.makedirs("_temp", exist_ok=True)


def _load_audio_for_inference(file_path: str) -> dict:
    """Load audio into memory to avoid pyannote's TorchCodec file decoder path."""
    waveform, sample_rate = torchaudio.load(file_path)
    return {"waveform": waveform, "sample_rate": sample_rate}


def embedding_endpoint_v2(file: UploadFile):
    """Extract a speaker embedding (wespeaker) from an uploaded audio file."""
    upload_id = str(uuid.uuid4())
    filename = os.path.basename(file.filename)
    file_path = f"_temp/{upload_id}_{filename}"
    try:
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        _validate_audio_duration(file_path)
        embedding = embedding_inference_v2(_load_audio_for_inference(file_path))
        return embedding.tolist()
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
