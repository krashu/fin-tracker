"""Streaming in-memory multipart reader for upload routes (ADR-0010 rule 13; PRD §F1).

FastAPI's Form()/File() parsing runs Starlette's MultiPartParser with its class default
spool_max_size (1 MB): a larger file part rolls over to a temp file on disk, and the whole body
is read before a handler can check its size. A route that must keep an upload in memory declares
no Form()/File() parameters and calls ``read_upload`` (or ``read_multipart``) from a dependency.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

TOO_LARGE = "file too large"


async def _counted(stream: AsyncIterator[bytes], max_bytes: int) -> AsyncGenerator[bytes]:
    seen = 0
    async for chunk in stream:
        seen += len(chunk)
        if seen > max_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=TOO_LARGE)
        yield chunk


async def read_multipart(request: Request, *, max_bytes: int, max_fields: int) -> FormData:
    """Parse the multipart body in memory; 413 the moment it passes ``max_bytes``.

    A declared Content-Length over the cap is refused before a byte is read, but never trusted
    (absent on chunked bodies, understatable), so the stream is counted as well. File parts sit
    in a spool sized to ``max_bytes``, which the counted body can never exceed: nothing rolls
    to disk. The caller closes the returned form.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=TOO_LARGE)
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="expected a multipart/form-data body"
        )
    parser = MultiPartParser(
        request.headers,
        _counted(request.stream(), max_bytes),
        max_files=1,
        max_fields=max_fields,
    )
    parser.spool_max_size = max_bytes  # this parser instance only, never the class attribute
    try:
        return await parser.parse()
    except MultiPartException as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid multipart body") from e


async def read_upload[M: BaseModel](
    request: Request, form_model: type[M], *, max_bytes: int, max_fields: int
) -> tuple[M, bytes]:
    """Read one ``file`` part plus the non-file fields, validated by ``form_model``.

    Validation failures are FastAPI's standard 422 and never echo the input (a field may be a
    password): ``from None`` because pydantic's ``str()`` quotes ``input_value``.
    """
    form = await read_multipart(request, max_bytes=max_bytes, max_fields=max_fields)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise RequestValidationError(
                [{"type": "missing", "loc": ("body", "file"), "msg": "Field required"}]
            )
        try:
            fields = form_model.model_validate(
                {k: v for k, v in form.multi_items() if isinstance(v, str)}
            )
        except ValidationError as e:
            raise RequestValidationError(
                [
                    {**err, "loc": ("body", *err["loc"])}
                    for err in e.errors(
                        include_input=False, include_url=False, include_context=False
                    )
                ]
            ) from None
        return fields, await upload.read()
    finally:
        await form.close()
