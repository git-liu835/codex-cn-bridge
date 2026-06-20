"""本地 Ollama 适配器 —— 兼容 OpenAI Chat Completions API

文档：https://github.com/ollama/ollama/blob/main/docs/openai.md
接口：POST http://localhost:11434/v1/chat/completions

Ollama 在本地运行开源模型（如 qwen3、deepseek-v3、llama3 等），
通过 OpenAI 兼容协议对外提供 API。API Key 可为任意值（默认使用 "ollama" 占位符）。
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class OllamaAdapter(BaseAdapter):
    name = "ollama"
    base_url = "http://localhost:11434/v1"
    api_key_env = "OLLAMA_API_KEY"  # 可为空
    unsupported_features: set[str] = set()  # Ollama 兼容 OpenAI Chat API
    supports_thinking_budget: bool = False  # Ollama 不支持 thinking

    capabilities: dict[str, bool | int] = {
        "tools": True,
        "streaming": True,
        "reasoning": False,  # 本地模型默认不支持思维链推理
        "vision": False,
        "image_gen": False,
        "video_gen": False,
        "code_execution": False,
        "max_tokens": 8192,
    }

    def get_headers(self, api_key: str) -> dict:
        """Ollama 不强制鉴权，API Key 为空时使用占位符

        Ollama 的 OpenAI 兼容端点要求 Authorization 头存在但忽略其值，
        因此当用户未配置 API Key 时使用 "ollama" 作为占位符。
        """
        if not api_key:
            api_key = "ollama"
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        # 移除 Ollama 不支持的字段
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        # stop 限制
        stop = chat_req.get("stop")
        if isinstance(stop, list) and len(stop) > 4:
            chat_req["stop"] = stop[:4]

        # Ollama 本地模型默认不支持思维链推理，强制关闭 thinking
        chat_req.pop("_disable_thinking", None)
        chat_req.pop("_thinking_budget", None)
        chat_req.pop("thinking", None)

        # 工具格式规范化
        tools = chat_req.get("tools")
        if tools:
            for tool in tools:
                if "function" not in tool and "name" in tool:
                    tool["function"] = {
                        "name": tool.pop("name"),
                        "description": tool.pop("description", ""),
                        "parameters": tool.pop("parameters", {}),
                    }
                if "type" not in tool:
                    tool["type"] = "function"
                params = tool.get("function", {}).get("parameters")
                if not params or not isinstance(params, dict):
                    tool.setdefault("function", {})["parameters"] = {"type": "object", "properties": {}}
                elif params.get("type") != "object":
                    params["type"] = "object"
                    params.setdefault("properties", {})

        return chat_req

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        """处理 Ollama 非流式响应"""
        choices = chat_resp.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if "type" not in tc:
                    tc["type"] = "function"
                func = tc.get("function", {})
                if "arguments" in func and isinstance(func["arguments"], dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)
        return chat_resp

    def stream_event_transform(self, raw_event: dict) -> dict:
        """Ollama SSE 格式标准化"""
        for choice in raw_event.get("choices", []):
            delta = choice.get("delta", {})
            tool_calls = delta.get("tool_calls", [])
            for tc in tool_calls:
                if "type" not in tc:
                    tc["type"] = "function"
                func = tc.get("function", {})
                if "arguments" in func and isinstance(func["arguments"], dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)
        return raw_event
