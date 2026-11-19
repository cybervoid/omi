import asyncio

import httpx
import pytest

from utils.stt import speaker_embedding


def _response(status: int, payload=None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload if payload is not None else {"error": "transient"},
        request=httpx.Request("POST", "https://embedding.invalid/v2/embedding"),
    )


def test_sync_embedding_retries_transient_500(monkeypatch):
    responses = iter([_response(500), _response(200, [0.1, 0.2])])
    calls = []
    sleeps = []
    monkeypatch.setattr(speaker_embedding, "EMBEDDING_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(speaker_embedding, "_retry_delay_seconds", lambda _attempt: 0.25)
    monkeypatch.setattr(speaker_embedding.time, "sleep", sleeps.append)

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr(speaker_embedding.httpx, "post", post)

    response = speaker_embedding._sync_embedding_response("https://embedding.invalid", "sample.wav", b"wav")

    assert response.status_code == 200
    assert len(calls) == 2
    assert sleeps == [0.25]


def test_sync_embedding_does_not_retry_nontransient_422(monkeypatch):
    calls = []
    monkeypatch.setattr(speaker_embedding, "EMBEDDING_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(speaker_embedding.time, "sleep", lambda _delay: pytest.fail("must not sleep"))

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return _response(422)

    monkeypatch.setattr(speaker_embedding.httpx, "post", post)

    with pytest.raises(httpx.HTTPStatusError):
        speaker_embedding._sync_embedding_response("https://embedding.invalid", "sample.wav", b"wav")
    assert len(calls) == 1


def test_async_embedding_retries_request_error(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = 0

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError(
                    "cold start",
                    request=httpx.Request("POST", "https://embedding.invalid/v2/embedding"),
                )
            return _response(200, [0.1, 0.2])

    client = Client()
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(speaker_embedding, "EMBEDDING_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(speaker_embedding, "_retry_delay_seconds", lambda _attempt: 0.5)
    monkeypatch.setattr(speaker_embedding.asyncio, "sleep", sleep)

    response = asyncio.run(
        speaker_embedding._async_embedding_response(client, "https://embedding.invalid", "sample.wav", b"wav")
    )

    assert response.status_code == 200
    assert client.calls == 2
    assert sleeps == [0.5]
