"""腾讯混元 (Hunyuan) 适配器 —— 支持 HY3-Preview 旗舰模型

HY3-Preview (2026-05):
  - 276B MoE, ~20B active
  - 上下文: 1M (SiliconFlow), 128K+ (Tencent Cloud)
  - thinking: 默认 OFF (reasoning_effort: "none") — 最安全的默认值
  - 控制: reasoning_effort: "high" | "medium" | "low" | "none"
  - 不支持 budget_tokens

旧模型 (hunyuan-t1-latest, hunyuan-2.0-thinking) 将于 2026-06-26 下线。
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class HunyuanAdapter(BaseAdapter):
    name = "hunyuan"
    base_url = "https://api.hunyuan.cloud.tencent.com/v1"
    api_key_env = "HUNYUAN_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = False  # 混元不支持 budget_tokens
    thinking_mode: str = "off"  # HY3 默认不思考, 最安全

    capabilities: dict[str, bool | int] = {
        "tools": True,
        "streaming": True,
        "reasoning": True,
        "vision": True,
        "image_gen": False,
        "video_gen": False,
        "code_execution": False,
        "max_tokens": 8192,
    }

    def apply_thinking(self, chat_req: dict) -> dict:
        """混元 HY3 thinking 控制 → reasoning_effort

        HY3 默认 reasoning_effort: "none" (关闭思考),
        只有显式请求时才开启。
        """
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
            if "reasoning_effort" not in chat_req:
                chat_req["reasoning_effort"] = "none"
            return chat_req

        budget = chat_req.pop("_thinking_budget", 4096)

        if "reasoning_effort" not in chat_req:
            if budget <= 2048:
                chat_req["reasoning_effort"] = "none"
            elif budget <= 8192:
                chat_req["reasoning_effort"] = "medium"
            else:
                chat_req["reasoning_effort"] = "high"

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
