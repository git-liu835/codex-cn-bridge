"""智谱 GLM 适配器 —— 支持 GLM-5.2 / GLM-5.1 / GLM-5 / GLM-4.7 等

GLM-5.2 (2026-06):
  - 744B MoE, ~40B active per token
  - 上下文: 200K tokens
  - max_output: 128K tokens
  - thinking: enabled by default, 通过 reasoning_effort 控制深度
  - reasoning_effort: "max" (default), "high", "none" (相当于 disabled)
  - 不支持 budget_tokens
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class GlmAdapter(BaseAdapter):
    name = "zhipu"
    base_url = "https://open.bigmodel.cn/api/paas/v4"
    api_key_env = "ZHIPU_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = False  # GLM 不支持 budget_tokens
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
        """GLM-5.x thinking 控制 —— 映射 effort 到 reasoning_effort

        Codex Responses API reasoning.effort:
          low    → reasoning_effort: "none"    (关闭思考，秒回)
          medium → reasoning_effort: "high"    (适度思考)
          high   → reasoning_effort: "max"     (深度思考，默认)

        _disable_thinking → reasoning_effort: "none"
        """
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
            if "reasoning_effort" not in chat_req:
                chat_req["reasoning_effort"] = "none"
            return chat_req

        budget = chat_req.pop("_thinking_budget", 4096)

        # 已显式设置 reasoning_effort → 保留
        if "reasoning_effort" in chat_req:
            return chat_req

        # 通过 budget 推断 effort 级别
        if budget <= 2048:
            chat_req["reasoning_effort"] = "none"
        elif budget <= 8192:
            chat_req["reasoning_effort"] = "high"
        else:
            chat_req["reasoning_effort"] = "max"

        # 关键保护: 强制设置 max_tokens 兜底，防止无限思考
        cur_max = chat_req.get("max_tokens", 0)
        min_max = budget + 12288  # thinking budget + 正文空间
        if cur_max and cur_max < min_max:
            chat_req["max_tokens"] = min_max
        elif not cur_max:
            chat_req["max_tokens"] = min_max

        return chat_req

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)

        # thinking 控制委托给 apply_thinking
        chat_req = self.apply_thinking(chat_req)

        if "do_sample" not in chat_req:
            chat_req["do_sample"] = True

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
