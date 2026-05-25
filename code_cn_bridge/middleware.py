"""中间件 —— 错误处理、请求日志、API Key 安全过滤、详细日志捕获"""

from __future__ import annotations

import json
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .models import build_error_response
from .stats import get_stats

logger = logging.getLogger("code-cn-bridge")


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """统一错误处理中间件"""

    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            logger.exception("未处理的异常: %s", exc)
            return JSONResponse(
                content=build_error_response(str(exc)),
                status_code=500,
            )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件（不记录 API Key）"""

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()

        # 跳过健康检查日志
        if request.url.path == "/health":
            return await call_next(request)

        logger.info("→ %s %s", request.method, request.url.path)

        response = await call_next(request)

        elapsed = (time.monotonic() - start) * 1000
        logger.info(
            "← %s %s → %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response


class ApiKeyFilter(logging.Filter):
    """过滤日志中的 API Key"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, dict):
            record.args = {
                k: ("***" if "key" in k.lower() or "token" in k.lower() else v)
                for k, v in record.args.items()
            }
        return True


def _redact_headers(headers: dict) -> dict:
    """脱敏请求头中的敏感字段"""
    sensitive = {"authorization", "api-key", "x-api-key", "cookie", "set-cookie"}
    return {
        k: "***REDACTED***" if k.lower().replace("_", "-") in sensitive else v
        for k, v in headers.items()
    }


class DetailedLoggingMiddleware:
    """ASGI 中间件 —— 捕获 /v1/ 端口的完整请求和响应（详细日志）"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        stats = get_stats()
        if not path.startswith("/v1/") or not stats.detailed_enabled:
            await self.app(scope, receive, send)
            return

        # 拦截 receive/send 以捕获 body
        request_chunks: list[bytes] = []
        response_chunks: list[bytes] = []
        response_status: list[int] = [200]
        response_headers: list[tuple[bytes, bytes]] = []
        start = time.monotonic()

        async def _receive():
            msg = await receive()
            if msg.get("type") == "http.request":
                body = msg.get("body", b"")
                if body:
                    request_chunks.append(body)
            return msg

        async def _send(msg):
            t = msg.get("type", "")
            if t == "http.response.start":
                response_status[0] = msg.get("status", 200)
            elif t == "http.response.body":
                body = msg.get("body", b"")
                if body:
                    response_chunks.append(body)
            await send(msg)

        try:
            await self.app(scope, _receive, _send)
        finally:
            elapsed = (time.monotonic() - start) * 1000

            req_body = b"".join(request_chunks)
            resp_body = b"".join(response_chunks)

            req_text = req_body.decode("utf-8", errors="replace")
            if len(req_text) > 8192:
                req_text = req_text[:8192] + "...(truncated)"

            resp_text = resp_body.decode("utf-8", errors="replace")
            if len(resp_text) > 8192:
                resp_text = resp_text[:8192] + "...(truncated)"

            # 尝试格式化 JSON
            try:
                parsed = json.loads(req_text)
                req_text = json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                pass

            stats.record_detailed_log({
                "timestamp": start,
                "time": time.strftime("%H:%M:%S", time.localtime(start)),
                "method": scope.get("method", "?"),
                "path": path,
                "query": scope.get("query_string", b"").decode("utf-8", errors="replace"),
                "request_body": req_text,
                "response_status": response_status[0],
                "response_body": resp_text,
                "elapsed_ms": round(elapsed, 1),
            })
