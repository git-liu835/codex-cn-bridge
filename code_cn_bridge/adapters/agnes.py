"""Agnes AI 适配器 —— 兼容 OpenAI V1 协议的全模态 API

文档与控制台：
- 开发者平台: https://platform.agnes-ai.com/
- 官方文档: https://agnes-ai.com/doc

接口兼容 OpenAI V1：
- Chat:      POST /v1/chat/completions
- Images:    POST /v1/images/generations
- Videos:    POST /v1/videos/generations

常用模型：
- 文本: agnes-2.0-flash, agnes-1.5-flash
- 图像: agnes-image-2.0-flash, agnes-image-2.1-flash
- 视频: agnes-video-v2.0
"""

from __future__ import annotations

import json

from .base import BaseAdapter


class AgnesAdapter(BaseAdapter):
    name = "agnes"
    base_url = "https://apihub.agnes-ai.com/v1"
    api_key_env = "AGNES_API_KEY"
    unsupported_features: set[str] = set()  # Agnes 对 Chat API 支持完整
    supports_thinking_budget: bool = False  # 仅支持 thinking 开关，不支持 budget_tokens

    capabilities: dict[str, bool | int] = {
        "tools": True,
        "streaming": True,
        "reasoning": True,
        "vision": True,
        "image_gen": True,
        "video_gen": True,
        "code_execution": False,
        "max_tokens": 8192,
    }

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        # 移除 Agnes 不支持的字段
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        # Agnes 支持 thinking 开关，可通过 model_mapping 中 enable_thinking 按模型关闭
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

        # stop 限制（兼容 OpenAI 标准，但部分网关对数组长度有限制）
        stop = chat_req.get("stop")
        if isinstance(stop, list) and len(stop) > 4:
            chat_req["stop"] = stop[:4]

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
        """处理 Agnes 非流式响应"""
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
        """Agnes SSE 格式标准化"""
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

    def preprocess_image_gen_request(self, req: dict) -> dict:
        """Agnes 图像生成兼容 DALL-E 格式"""
        req.setdefault("response_format", "url")
        return req
