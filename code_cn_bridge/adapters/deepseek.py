"""DeepSeek 适配器 —— 支持 V4-Pro / V4-Flash (2026-04)

DeepSeek V4-Pro:
  - 1.6T MoE, 49B active
  - 上下文: 1M tokens, max_output: 384K
  - thinking 三模式: non-thinking, thinking (Think High), thinking_max (Think Max)
  - 通过 thinking_mode 参数控制, 不支持 budget_tokens
  - Anthropic 兼容端点支持 thinking.budget_tokens, 主流端点不支持

DeepSeek V4-Flash:
  - 284B MoE, 13B active
  - 同样支持 thinking 三模式
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class DeepSeekAdapter(BaseAdapter):
    name = "deepseek"
    base_url = "https://api.deepseek.com"
    api_key_env = "DEEPSEEK_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = False  # DeepSeek 主流端点不支持 budget_tokens
    thinking_mode: str = "auto"

    capabilities: dict[str, bool | int] = {
        "tools": True,
        "streaming": True,
        "reasoning": True,
        "vision": False,
        "image_gen": False,
        "video_gen": False,
        "code_execution": False,
        "max_tokens": 8192,
    }

    def apply_thinking(self, chat_req: dict) -> dict:
        """DeepSeek V4+ thinking 控制 → thinking_mode 参数

        effort 映射:
          low    → thinking_mode: "non-thinking"
          medium → thinking_mode: "thinking"       (Think High)
          high   → thinking_mode: "thinking_max"   (Think Max)
        """
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
            if "thinking_mode" not in chat_req:
                chat_req["thinking_mode"] = "non-thinking"
            return chat_req

        budget = chat_req.pop("_thinking_budget", 4096)

        if "thinking_mode" in chat_req:
            return chat_req

        # budget → effort → thinking_mode
        if budget <= 2048:
            chat_req["thinking_mode"] = "non-thinking"
        elif budget <= 16384:
            chat_req["thinking_mode"] = "thinking"
        else:
            chat_req["thinking_mode"] = "thinking_max"

        # 关键: max_tokens 兜底, thinking_max 可耗尽 384K
        cur_max = chat_req.get("max_tokens", 0)
        min_max = budget + 16384
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

    def extract_tool_calls_from_content(self, content: str) -> list[dict] | None:
        return None
