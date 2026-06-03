"""HTTP 客户端 —— 异步转发请求到国产模型 API"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from .adapters.base import BaseAdapter

logger = logging.getLogger(__name__)


class UpstreamClient:
    """上游模型 API 异步客户端 —— 支持多 key 轮转"""

    def __init__(self, adapter: BaseAdapter, api_keys: list[str], timeout: float = 120.0, stream_timeout: float = 600.0):
        self.adapter = adapter
        self._api_keys = api_keys if isinstance(api_keys, list) else [api_keys]
        self._key_index = 0
        self._client: httpx.AsyncClient | None = None
        self._stream_client: httpx.AsyncClient | None = None
        self._timeout = timeout
        self._stream_timeout = stream_timeout

    @property
    def current_key(self) -> str:
        return self._api_keys[self._key_index] if self._api_keys else ""

    def rotate_key(self) -> str:
        """轮转到下一个 API key，返回新的 key"""
        if len(self._api_keys) <= 1:
            return self.current_key
        self._key_index = (self._key_index + 1) % len(self._api_keys)
        logger.info("API key 轮转: provider=%s, key_index=%d/%d",
            self.adapter.name, self._key_index + 1, len(self._api_keys))
        # 轮转时重置 HTTP 客户端（不同 key 不应复用连接）
        self._client = None
        self._stream_client = None
        return self.current_key

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
                trust_env=False,  # 绕过系统代理，直连 API
            )
        return self._client

    async def _get_stream_client(self) -> httpx.AsyncClient:
        """流式请求专用客户端 —— 读超时更长，容忍模型长时间推理"""
        if self._stream_client is None:
            self._stream_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=self._stream_timeout, write=30.0, pool=30.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
                trust_env=False,  # 绕过系统代理，直连 API
            )
        return self._stream_client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._stream_client:
            await self._stream_client.aclose()
            self._stream_client = None

    async def chat_completion(self, chat_req: dict) -> dict:
        """发送非流式 Chat Completions 请求"""
        client = await self._get_client()
        url = self.adapter.build_chat_url()
        headers = self.adapter.get_headers(self.current_key)

        response = await client.post(url, json=chat_req, headers=headers)
        if response.status_code >= 400:
            body = await response.aread()
            from .models import normalize_upstream_error
            err_msg = normalize_upstream_error(body.decode(), response.status_code)
            raise httpx.HTTPStatusError(
                err_msg,
                request=response.request,
                response=response,
            )
        return response.json()

    async def chat_completion_stream(self, chat_req: dict) -> AsyncIterator[dict]:
        """发送流式 Chat Completions 请求，返回 SSE 事件迭代器（单次，不重试）

        重试逻辑由调用方（server.py）处理，确保每次重试可以重建 StreamTranslator。
        """
        async for chunk in self._stream_once(chat_req):
            yield chunk

    async def _stream_once(self, chat_req: dict) -> AsyncIterator[dict]:
        """单次流式请求"""
        client = await self._get_stream_client()
        url = self.adapter.build_chat_url()
        headers = self.adapter.get_headers(self.current_key)
        chat_req["stream"] = True

        async with client.stream("POST", url, json=chat_req, headers=headers) as response:
            if response.status_code >= 400:
                body = await response.aread()
                from .models import normalize_upstream_error
                err_msg = normalize_upstream_error(body.decode(), response.status_code)
                raise httpx.HTTPStatusError(
                    err_msg,
                    request=response.request,
                    response=response,
                )
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        yield chunk
                    except json.JSONDecodeError:
                        continue
