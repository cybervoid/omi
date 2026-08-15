"""Minimal diarizer service for Cloud Run: only the /v2/embedding endpoint the
Omi backend calls (via HOSTED_SPEAKER_EMBEDDING_API_URL), plus a health check."""

import logging

from fastapi import FastAPI, UploadFile, File

from embedding import embedding_endpoint_v2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.post("/v2/embedding")
def embedding_v2(file: UploadFile = File(...)):
    logger.info("embedding v2")
    return embedding_endpoint_v2(file)


@app.get("/health")
def health_check():
    return {"status": "healthy"}
