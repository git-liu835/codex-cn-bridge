"""Moonshot (Kimi) 适配器 —— 支持 K2.7-Code / K2.6 / K2.5

Kimi K2.7-Code (2026-06-12):
  - 1T total MoE, 32B active, 384 experts
  - 上下文: 262K tokens
  - thinking: FORCED ON — 无法关闭
  - 发送 thinking: {type: "disabled"} 会被 API 拒绝
  - 不支持 budget_tokens

Kimi K2.6 / K2.5:
  - thinking 可开关: thinking: {type: "enabled"|"disabled"}
  - preserve_thinking: true 跨多轮保留推理内容
"""

from __future__ import annotations

import json
import re

from .base import BaseAdapter


class KimiAdapter(BaseAdapter):
    name = "kimi"
    base_url = "https://api.moonshot.cn/v1"
    api_key_env = "KIMI_API_KEY"
    unsupported_features: set[str] = set()
    supports_thinking_budget: bool = False  # Kimi 不支持 budget_tokens
    thinking_mode: str = "forced"  # K2.7 Code 强制开启

    capabilities: dict[str, bool | int] = {
        "tools": True,
        "streaming": True,
        "reasoning": True,
        "vision": False,
        "image_gen": False,
        "video_gen": False,
        "code_execution": False,
        "max_tokens": 8192,
    }

    def apply_thinking(self, chat_req: dict) -> dict:
        """Kimi K2.7 Code: thinking 强制开启, API 拒绝 disabled

        effort 映射 (仅影响 max_tokens 兜底值, 不影响 thinking 状态):
          low    → max_tokens: budget + 12288
          medium → max_tokens: budget + 20480
          high   → max_tokens: budget + 36864
        """
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            chat_req.pop("_thinking_budget", None)
            # K2.7 Code 不能发送 disabled, 但仍可控制 max_tokens
            if "thinking" not in chat_req:
                chat_req["thinking"] = {"type": "enabled"}
            cur_max = chat_req.get("max_tokens", 0)
            if not cur_max or cur_max < 12288:
                chat_req["max_tokens"] = 12288
            return chat_req

        budget = chat_req.pop("_thinking_budget", 4096)

        if "thinking" not in chat_req:
            chat_req["thinking"] = {"type": "enabled"}

        # 多轮保留推理上下文
        if "preserve_thinking" not in chat_req:
            chat_req["preserve_thinking"] = True

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

        chat_req = self.apply_thinking(chat_req)

        tools = chat_req.get("tools")
        if tools:
            if not self.supports_tool_calls():
                chat_req.pop("tools", None)
                chat_req.pop("tool_choice", None)
                chat_req = self._inject_tools_as_prompt(chat_req)

        return chat_req

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        choices = chat_resp.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            content = msg.get("content", "")

            if content and isinstance(content, str) and not msg.get("tool_calls"):
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

    def supports_tool_calls(self) -> bool:
        return True

    def extract_tool_calls_from_content(self, content: str) -> list[dict] | None:
        if not content:
            return None

        patterns = [
            r"<function_call>\s*(.*?)\s*</function_call>",
            r'```json\s*(\{.*?"name".*?\})\s*```',
            r'<tool_call>\s*(.*?)\s*</tool_call>',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, content, re.DOTALL)
            if matches:
                tool_calls = []
                for i, m in enumerate(matches):
                    try:
                        data = json.loads(m)
                        name = data.get("name", data.get("function", ""))
                        args = data.get("arguments", data.get("parameters", {}))
                        tool_calls.append({
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                            },
                        })
                    except json.JSONDecodeError:
                        continue

                if tool_calls:
                    return tool_calls

        return None

    def _inject_tools_as_prompt(self, chat_req: dict) -> dict:
        tools = chat_req.get("tools", [])
        if not tools:
            return chat_req

        tool_descs = []
        for tool in tools:
            fn = tool.get("function", tool)
            name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            tool_descs.append(f"- {name}: {desc}\n  Parameters: {json.dumps(params, ensure_ascii=False)}")

        tool_prompt = (
            "\n\n你可以在回复中通过 JSON 格式调用以下函数：\n"
            + "\n".join(tool_descs)
            + '\n\n调用格式：\n<function_call>\n{"name": "函数名", "arguments": {...}}\n</function_call>'
        )

        messages = chat_req.get("messages", [])
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = (messages[0].get("content", "") + tool_prompt)
        else:
            messages.insert(0, {"role": "system", "content": tool_prompt.lstrip()})

        chat_req["messages"] = messages
        return chat_req
