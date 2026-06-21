"""硅基流动 (SiliconFlow) 适配器 —— 聚合推理平台

SiliconFlow 聚合多家模型 (DeepSeek V4, Qwen3, GLM-5, HY3 等),
通过统一 OpenAI 兼容协议对外提供。

thinking 控制约定 (与各原生厂商不同):
  - 使用 enable_thinking: true/false (不是 thinking object)
  - Qwen3 支持 thinking_budget 参数
  - DeepSeek V4+ 支持 reasoning_effort: "think_max" | "think_high" | "non_think"
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class SiliconFlowAdapter(BaseAdapter):
    name = "siliconflow"
    base_url = "https://api.siliconflow.cn/v1"
    api_key_env = "SILICONFLOW_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = True  # SF 支持 enable_thinking + thinking_budget
    thinking_mode: str = "auto"

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
        """SiliconFlow 统一的 thinking 控制

        与原生厂商不同, SF 使用 enable_thinking + thinking_budget。
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
