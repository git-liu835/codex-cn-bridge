"""百度文心 (ERNIE) 适配器 —— 千帆 V2 平台, 支持 ERNIE 5.1

ERNIE 5.1 (2026-05):
  - 800B MoE, text-only
  - 上下文: 128K, max_output: 65K
  - thinking: 不支持独立思考模式 (无 reasoning_content)
  - 推理能力来自 agentic post-training + tool augmentation
  - 成本极低: $0.59/M in, $2.65/M out
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class ErnieAdapter(BaseAdapter):
    name = "ernie"
    base_url = "https://qianfan.baidubce.com/v2"
    api_key_env = "ERNIE_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = False
    thinking_mode: str = "off"  # ERNIE 不支持 thinking

    capabilities: dict[str, bool | int] = {
        "tools": True,
        "streaming": True,
        "reasoning": False,  # 不支持思维链推理
        "vision": True,
        "image_gen": False,
        "video_gen": False,
        "code_execution": False,
        "max_tokens": 8192,
    }

    def apply_thinking(self, chat_req: dict) -> dict:
        """ERNIE 5.1 不支持 thinking 模式, 强制移除"""
        chat_req.pop("_disable_thinking", None)
        chat_req.pop("_thinking_budget", None)
        chat_req.pop("thinking", None)
        chat_req.pop("reasoning_effort", None)
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
