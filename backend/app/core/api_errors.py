from __future__ import annotations

import re
import uuid

from fastapi import FastAPI, HTTPException as FastAPIHTTPException, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(FastAPIHTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message


def api_error(status: int, code: str, message: str) -> ApiError:
    return ApiError(status, code, message)


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def install_api_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        if isinstance(exc, ApiError):
            code = exc.code
            message = exc.message
        else:
            code = f"HTTP_{exc.status_code}"
            message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        headers = dict(exc.headers or {})
        headers["X-Request-ID"] = request_id
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "code": code,
                "message": message,
                "request_id": request_id,
            },
            headers=headers,
        )
