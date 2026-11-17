"""Unit tests for ``app.api.uploads.read_multipart`` (ADR-0010 rule 13; PRD §F1 Upload limits).

Each test builds a Starlette ``Request`` over a hand-rolled ASGI ``receive`` that counts how
many body chunks the reader pulled, so "refused before the body was read in full" is measured,
not assumed.
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.requests import Request

from app.api.uploads import TOO_LARGE, read_multipart

_CHUNK = 64 * 1024
_MIB = 1024 * 1024
_BOUNDARY = "xBOUNDARYx"


class _Receive:
    """ASGI ``receive`` serving ``chunks`` (then endless filler if ``endless``); counts calls."""

    def __init__(self, chunks: list[bytes], *, endless: bool = False) -> None:
        self._chunks = chunks
        self._endless = endless
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self._endless and not self._chunks:
            return {"type": "http.request", "body": b"x" * _CHUNK, "more_body": True}
        if not self._chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        body = self._chunks.pop(0)
        return {
            "type": "http.request",
            "body": body,
            "more_body": bool(self._chunks) or self._endless,
        }


def _request(receive: _Receive, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope, receive)


_MULTIPART = {"content-type": f"multipart/form-data; boundary={_BOUNDARY}"}


def _body(file_bytes: bytes) -> bytes:
    return (
        (
            f"--{_BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="account_id"\r\n\r\n7\r\n'
            f"--{_BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="file"; filename="s.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + file_bytes
        + f"\r\n--{_BOUNDARY}--\r\n".encode()
    )


def _endless_upload() -> _Receive:
    head = _body(b"")[: _body(b"").index(b"\r\n--" + _BOUNDARY.encode() + b"--")]
    return _Receive([head], endless=True)


def _chunked(body: bytes) -> list[bytes]:
    return [body[i : i + _CHUNK] for i in range(0, len(body), _CHUNK)]


def test_over_cap_body_is_413_before_it_is_read_in_full() -> None:
    receive = _endless_upload()  # a valid start, then file bytes forever, and no Content-Length
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_multipart(_request(receive, _MULTIPART), max_bytes=_MIB, max_fields=2))
    assert exc.value.status_code == 413
    assert exc.value.detail == TOO_LARGE
    assert receive.calls <= _MIB // _CHUNK + 2


def test_declared_over_cap_content_length_is_413_without_reading() -> None:
    receive = _endless_upload()
    headers = {**_MULTIPART, "content-length": str(_MIB + 1)}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_multipart(_request(receive, headers), max_bytes=_MIB, max_fields=2))
    assert exc.value.status_code == 413
    assert receive.calls == 0


def test_understated_content_length_does_not_bypass_the_count() -> None:
    receive = _endless_upload()
    headers = {**_MULTIPART, "content-length": "100"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_multipart(_request(receive, headers), max_bytes=_MIB, max_fields=2))
    assert exc.value.status_code == 413
    assert receive.calls <= _MIB // _CHUNK + 2


def test_file_part_stays_in_memory_up_to_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    rollovers: list[object] = []
    monkeypatch.setattr(
        tempfile.SpooledTemporaryFile, "rollover", lambda self: rollovers.append(self)
    )
    payload = b"%PDF-1.4 " + b"A" * (2 * _MIB)
    receive = _Receive(_chunked(_body(payload)))

    async def go() -> tuple[str, bytes]:
        form = await read_multipart(
            _request(receive, _MULTIPART), max_bytes=10 * _MIB, max_fields=2
        )
        try:
            upload = form["file"]
            assert isinstance(upload, UploadFile)
            return str(form["account_id"]), await upload.read()
        finally:
            await form.close()

    account_id, data = asyncio.run(go())
    assert (account_id, data) == ("7", payload)
    assert rollovers == []


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "application/json"},
        {"content-type": "multipart/form-data"},  # no boundary
    ],
)
def test_non_multipart_or_malformed_body_is_400(headers: dict[str, str]) -> None:
    receive = _Receive([b"{}"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_multipart(_request(receive, headers), max_bytes=_MIB, max_fields=2))
    assert exc.value.status_code == 400
