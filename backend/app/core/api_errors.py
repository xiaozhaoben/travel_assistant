from __future__ import annotations

import logging
import re
import uuid

from fastapi import FastAPI, HTTPException as FastAPIHTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


logger = logging.getLogger(__name__)


class ApiError(FastAPIHTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message


def api_error(status: int, code: str, message: str) -> ApiError:
    return ApiError(status, code, message)


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HTTP_ERROR_MESSAGES = {
    400: "请求无效",
    401: "身份认证失败",
    403: "无权访问",
    404: "资源不存在",
    405: "请求方法不支持",
    409: "请求冲突",
    422: "请求参数校验失败",
    429: "请求过于频繁",
    503: "服务暂时不可用",
}


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
        if isinstance(exc, ApiError):
            code = exc.code
            message = exc.message
        else:
            code = f"HTTP_{exc.status_code}"
            message = _HTTP_ERROR_MESSAGES.get(exc.status_code, "请求失败")
        return _error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError):
        return _error_response(
            request,
            status_code=422,
            code="REQUEST_VALIDATION_FAILED",
            message="请求参数校验失败",
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled application error")
        return _error_response(
            request,
            status_code=500,
            code="INTERNAL_ERROR",
            message="服务器内部错误",
        )


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    response_headers = dict(headers or {})
    response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "code": code,
            "message": message,
            "request_id": request_id,
        },
        headers=response_headers,
    )
