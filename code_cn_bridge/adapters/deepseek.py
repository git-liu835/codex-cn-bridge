"""DeepSeek 适配器 —— 支持 V4-Pro / V4-Flash / deepseek-chat

官方文档 (https://api-docs.deepseek.com/):
  - base_url: https://api.deepseek.com 或 https://api.deepseek.com/v1
  - model: deepseek-chat / deepseek-reasoner（兼容名）
           deepseek-v4-pro / deepseek-v4-flash（当前正式名）
  - thinking 控制: thinking: {type: "enabled"|"disabled"}
  - effort 控制: reasoning_effort: "high"|"max"
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class DeepSeekAdapter(BaseAdapter):
    name = "deepseek"
    base_url = "https://api.deepseek.com/v1"
    api_key_env = "DEEPSEEK_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = False  # 官方 OpenAI 兼容端点用 reasoning_effort，不用 budget_tokens
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
        """DeepSeek V4 thinking 控制 → 官方 thinking + reasoning_effort

        映射:
          disable / low budget → thinking.disabled
          medium               → thinking.enabled + reasoning_effort=high
          high                 → thinking.enabled + reasoning_effort=max
        """
        # 清掉历史错误字段，避免上游拒识
        chat_req.pop("thinking_mode", None)

        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
            chat_req["thinking"] = {"type": "disabled"}
            chat_req.pop("reasoning_effort", None)
            return chat_req

        budget = chat_req.pop("_thinking_budget", 4096)

        # 已显式设置 thinking → 只补齐 effort
        if "thinking" in chat_req:
            thinking = chat_req.get("thinking")
            if isinstance(thinking, dict) and thinking.get("type") == "disabled":
                chat_req.pop("reasoning_effort", None)
                return chat_req
            if "reasoning_effort" not in chat_req:
                chat_req["reasoning_effort"] = "high" if budget <= 16384 else "max"
            return chat_req

        if budget <= 2048:
            chat_req["thinking"] = {"type": "disabled"}
            chat_req.pop("reasoning_effort", None)
        else:
            chat_req["thinking"] = {"type": "enabled"}
            # 官方：low/medium 映射为 high；更高预算用 max
            chat_req["reasoning_effort"] = "high" if budget <= 16384 else "max"

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
