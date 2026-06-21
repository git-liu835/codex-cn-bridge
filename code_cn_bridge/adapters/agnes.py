"""Agnes AI 适配器 —— 全模态 API, 支持 Agnes 2.0-Flash

Agnes 2.0-Flash (2026-06):
  - 上下文: 1M, max_output: 65K
  - thinking: 支持, OpenAI endpoint 用 enable_thinking
  - Anthropic endpoint 支持 budget_tokens (推荐 2048-8192)
  - 完全免费 (永久免费层)

Agnes 1.5-Flash:
  - 不支持 thinking, 不支持 tool calling
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class AgnesAdapter(BaseAdapter):
    name = "agnes"
    base_url = "https://apihub.agnes-ai.com/v1"
    api_key_env = "AGNES_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = True  # Anthropic 端点原生支持
    thinking_mode: str = "auto"

    capabilities: dict[str, bool | int] = {
        "tools": True,
        "streaming": True,
        "reasoning": True,
        "vision": True,
        "image_gen": True,
        "video_gen": True,
        "code_execution": False,
        "max_tokens": 8192,
    }

    def apply_thinking(self, chat_req: dict) -> dict:
        """Agnes thinking 控制

        OpenAI 兼容端点用 enable_thinking + thinking_budget。
        """
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
            chat_req["enable_thinking"] = False
            return chat_req

        budget = chat_req.pop("_thinking_budget", 4096)

        if "enable_thinking" not in chat_req:
            if budget <= 2048:
                chat_req["enable_thinking"] = False
            else:
                chat_req["enable_thinking"] = True

        if "thinking_budget" not in chat_req and chat_req.get("enable_thinking"):
            chat_req["thinking_budget"] = budget

        cur_max = chat_req.get("max_tokens", 0)
        min_max = budget + 12288
        if cur_max and cur_max < min_max:
            chat_req["max_tokens"] = min_max
        elif not cur_max:
            chat_req["max_tokens"] = min_max

        return chat_req

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

    def preprocess_image_gen_request(self, req: dict) -> dict:
        req.setdefault("response_format", "url")
        return req
