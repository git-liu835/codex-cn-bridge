"""百度文心 (ERNIE) 适配器 —— 千帆 V2 平台，兼容 OpenAI Chat Completions API

文档：https://cloud.baidu.com/doc/WENXINWORKSHOP/index
接口：POST https://qianfan.baidubce.com/v2/chat/completions
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class ErnieAdapter(BaseAdapter):
    name = "ernie"
    base_url = "https://qianfan.baidubce.com/v2"
    api_key_env = "ERNIE_API_KEY"
    unsupported_features: set[str] = set()  # 千帆 V2 对 Chat API 支持完整
    supports_thinking_budget: bool = False  # ERNIE 仅支持 thinking 开关，不支持 budget_tokens

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

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        # 移除 ERNIE 不支持的字段
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        # stop 限制
        stop = chat_req.get("stop")
        if isinstance(stop, list) and len(stop) > 4:
            chat_req["stop"] = stop[:4]

        # ERNIE 支持 thinking 开关，可通过 model_mapping 中 enable_thinking: false 按模型关闭
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
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

        # 工具格式规范化
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
        """处理 ERNIE 非流式响应"""
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
        """ERNIE SSE 格式标准化"""
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
