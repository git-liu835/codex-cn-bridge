"""通义千问 (Qwen) 适配器 —— 支持 Qwen3.7-Max / Qwen3.7-Plus / Qwen3-Coder-Plus

Qwen3.7-Max (2026-05):
  - 1M 上下文, 64K max_output
  - 混合推理模型: 支持 thinking 开关, 有 thinking_budget (上限 256K)
  - 控制参数: enable_thinking: true/false + thinking_budget: N
  - 思考 token 计入输出计费
"""

from __future__ import annotations

import json
import re

from .base import BaseAdapter


class QwenAdapter(BaseAdapter):
    name = "qwen"
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env = "QWEN_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = True  # Qwen3.x 支持 thinking_budget
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
        """Qwen3.7 thinking 控制 → enable_thinking + thinking_budget

        Qwen3.7 原生支持 thinking_budget, 直接映射。
        """
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
            chat_req["enable_thinking"] = False
            return chat_req

        budget = chat_req.pop("_thinking_budget", 4096)

        if "enable_thinking" not in chat_req:
            chat_req["enable_thinking"] = True

        if "thinking_budget" not in chat_req:
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
        if isinstance(stop, list):
            chat_req["stop"] = stop

        chat_req = self.apply_thinking(chat_req)

        return chat_req

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        choices = chat_resp.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            content = msg.get("content", "")

            if content and isinstance(content, str):
                extracted = self.extract_tool_calls_from_content(content)
                if extracted:
                    msg["tool_calls"] = extracted
                    msg["content"] = None

            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if "type" not in tc:
                    tc["type"] = "function"
                func = tc.get("function", {})
                if "arguments" in func and isinstance(func["arguments"], dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)

        return chat_resp

    def stream_event_transform(self, raw_event: dict) -> dict:
        if "output" in raw_event and "choices" not in raw_event:
            output = raw_event["output"]
            if isinstance(output, dict) and "choices" in output:
                raw_event["choices"] = output["choices"]

        if "choices" not in raw_event:
            return raw_event

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
        if not content:
            return None

        pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
        matches = re.findall(pattern, content, re.DOTALL)
        if not matches:
            return None

        tool_calls = []
        for i, m in enumerate(matches):
            try:
                data = json.loads(m)
                tool_calls.append({
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": data.get("name", ""),
                        "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                    },
                })
            except json.JSONDecodeError:
                fn_match = re.match(
                    r'(\w+)\s*\((.*)\)', m.strip(), re.DOTALL
                )
                if fn_match:
                    tool_calls.append({
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": fn_match.group(1),
                            "arguments": fn_match.group(2).strip(),
                        },
                    })

        return tool_calls if tool_calls else None
