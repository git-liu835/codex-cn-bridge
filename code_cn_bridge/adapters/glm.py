"""智谱 GLM 适配器 —— 支持 GLM-5.1 / GLM-5V-Turbo / GLM-4.7-Flash 等"""

from __future__ import annotations

import json

from .base import BaseAdapter


class GlmAdapter(BaseAdapter):
    name = "zhipu"
    base_url = "https://open.bigmodel.cn/api/paas/v4"
    api_key_env = "ZHIPU_API_KEY"
    unsupported_features: set[str] = set()  # GLM 对 Chat API 支持完整
    supports_thinking_budget: bool = False  # GLM 仅支持 thinking 开关，不支持 budget_tokens

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)

        # 启用 thinking 模式，利用模型推理能力
        # reasoning_content 会被 StreamTranslator 转换为 reasoning 输出项
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
            else:
                chat_req.pop("_thinking_budget", None)
                chat_req["thinking"] = {"type": "enabled"}
                # 确保 max_tokens 足够容纳 thinking + 实际输出
                cur_max = chat_req.get("max_tokens", 0)
                if not cur_max or cur_max < 16384:
                    chat_req["max_tokens"] = 16384

        # do_sample: 智谱默认采样模式
        if "do_sample" not in chat_req:
            chat_req["do_sample"] = True

        # 修复 tools 格式
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
