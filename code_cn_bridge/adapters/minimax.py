"""MiniMax 适配器 —— 兼容 OpenAI V1 协议, 支持 M3 旗舰模型

MiniMax-M3 (2026-05):
  - 428B MoE, ~23B active per token
  - 上下文: 1M tokens, max_output: 512K
  - thinking 三模式: disabled / adaptive (默认) / enabled
  - adaptive: 模型自我调节推理深度 (推荐, 最安全)
  - reasoning_split: true 分离推理到 reasoning_details
  - 多轮 tool calling 必须保留 assistant 完整响应 (含 reasoning_details)
  - 不支持 budget_tokens

接口: POST https://api.minimaxi.com/v1/chat/completions
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class MiniMaxAdapter(BaseAdapter):
    name = "minimax"
    base_url = "https://api.minimaxi.com/v1"
    api_key_env = "MINIMAX_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = False  # M3 不支持 budget_tokens
    thinking_mode: str = "adaptive"  # 默认 adaptive, 自我调节推理深度

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
        """MiniMax M3 thinking 控制 → thinking.type

        M3 特有的 adaptive 模式让模型自我决定何时需要推理，
        在 thinking+内容之间自动平衡。
        """
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
            if "thinking" not in chat_req:
                chat_req["thinking"] = {"type": "disabled"}
            return chat_req

        budget = chat_req.pop("_thinking_budget", 4096)

        if "thinking" not in chat_req:
            if budget <= 2048:
                chat_req["thinking"] = {"type": "disabled"}
            elif budget <= 8192:
                chat_req["thinking"] = {"type": "adaptive"}
            else:
                chat_req["thinking"] = {"type": "enabled"}

        # reasoning_split 干净分离推理和正文
        if "reasoning_split" not in chat_req:
            chat_req["reasoning_split"] = True

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
