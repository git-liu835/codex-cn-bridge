"""本地 Ollama 适配器 —— 兼容 OpenAI Chat Completions API

Ollama 运行本地开源模型 (GGUF), 通过 OpenAI 兼容协议暴露 API。
支持模型: deepseek-r1, qwen3, hunyuan-a13b 等。

thinking 控制:
  - 不由 Ollama 统一管理, 取决于底层模型
  - 通过 chat_template_kwargs 传递 (如 qwen3 的 enable_thinking)
  - 默认关闭 thinking, 本地模型 token 速度有限
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class OllamaAdapter(BaseAdapter):
    name = "ollama"
    base_url = "http://localhost:11434/v1"
    api_key_env = "OLLAMA_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = False
    thinking_mode: str = "off"  # 本地模型默认关闭 thinking

    capabilities: dict[str, bool | int] = {
        "tools": True,
        "streaming": True,
        "reasoning": False,
        "vision": False,
        "image_gen": False,
        "video_gen": False,
        "code_execution": False,
        "max_tokens": 8192,
    }

    def apply_thinking(self, chat_req: dict) -> dict:
        """Ollama 本地模型默认关闭 thinking

        本地模型 token 生成速度有限, 开启 thinking 易导致超长等待。
        """
        chat_req.pop("_disable_thinking", None)
        chat_req.pop("_thinking_budget", None)
        chat_req.pop("thinking", None)
        chat_req.pop("reasoning_effort", None)
        chat_req.pop("enable_thinking", None)
        return chat_req

    def get_headers(self, api_key: str) -> dict:
        if not api_key:
            api_key = "ollama"
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        stop = chat_req.get("stop")
        if isinstance(stop, list) and len(stop) > 4:
            chat_req["stop"] = stop[:4]

        chat_req = self.apply_thinking(chat_req)

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
