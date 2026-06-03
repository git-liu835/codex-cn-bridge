"""DeepSeek 适配器"""

from __future__ import annotations

import json

from .base import BaseAdapter


class DeepSeekAdapter(BaseAdapter):
    name = "deepseek"
    base_url = "https://api.deepseek.com"
    api_key_env = "DEEPSEEK_API_KEY"
    unsupported_features: set[str] = set()  # DeepSeek 对 Chat API 支持完整

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        # DeepSeek 不支持 logprobs
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        # stop 只支持单个字符串或字符串列表，DeepSeek 支持列表
        stop = chat_req.get("stop")
        if isinstance(stop, list) and len(stop) > 4:
            chat_req["stop"] = stop[:4]

        # 启用 thinking 模式，利用模型的推理能力
        # reasoning_content 会被 StreamTranslator 转换为 reasoning 输出项，
        # 不再转发为常规 content，因此不会导致推理循环
        # 可通过 model_mapping 中 enable_thinking: false 按模型关闭
        # budget_tokens 限制推理 token 数，防止模型耗尽所有 token 在推理上
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            if "thinking" not in chat_req:
                chat_req["thinking"] = {"type": "disabled"}
        elif "thinking" not in chat_req:
            if self.supports_thinking_budget:
                budget = chat_req.pop("_thinking_budget", 4096)
                chat_req["thinking"] = {"type": "enabled", "budget_tokens": budget}
                # 确保 max_tokens 足够容纳 thinking + 实际输出，否则模型可能只思考不输出
                # thinking_budget=8192 时 max_tokens 至少 24576（留 16384 给正文）
                cur_max = chat_req.get("max_tokens", 0)
                min_max = budget + 16384
                if cur_max and cur_max < min_max:
                    chat_req["max_tokens"] = min_max
                elif not cur_max:
                    chat_req["max_tokens"] = min_max
            else:
                chat_req.pop("_thinking_budget", None)
                chat_req["thinking"] = {"type": "enabled"}

        # DeepSeek 对 tool 格式有要求，确保 function 字段存在
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
                # 修复 parameters: 必须是 type: "object" 的 JSON Schema
                params = tool.get("function", {}).get("parameters")
                if not params or not isinstance(params, dict):
                    tool.setdefault("function", {})["parameters"] = {"type": "object", "properties": {}}
                elif params.get("type") != "object":
                    params["type"] = "object"
                    params.setdefault("properties", {})

        return chat_req

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        """处理 DeepSeek 非流式响应"""
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
        """DeepSeek SSE 格式基本标准，做微调"""
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
        return None  # DeepSeek 原生支持 tool_calls，无需提取
