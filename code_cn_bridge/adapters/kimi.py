"""Moonshot (Kimi) 适配器"""

from __future__ import annotations

import json
import re

from .base import BaseAdapter


class KimiAdapter(BaseAdapter):
    name = "kimi"
    base_url = "https://api.moonshot.cn/v1"
    api_key_env = "KIMI_API_KEY"

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        # Kimi 不支持 logprobs 和 logit_bias
        chat_req.pop("logprobs", None)
        chat_req.pop("logit_bias", None)
        chat_req.pop("user", None)

        # Kimi k2.x 默认启用 thinking，利用模型推理能力
        # reasoning_content 会被 StreamTranslator 转换为 reasoning 输出项
        # 历史消息中缺失 reasoning_content 的情况由 _flush_tool_calls 兜底处理
        # 可通过 model_mapping 中 enable_thinking: false 按模型关闭
        if "_disable_thinking" in chat_req:
            chat_req.pop("_disable_thinking")
            if "thinking" not in chat_req:
                chat_req["thinking"] = {"type": "disabled"}
        elif "thinking" not in chat_req:
            chat_req["thinking"] = {"type": "enabled"}

        # 老版本 Kimi 不支持 function calling
        tools = chat_req.get("tools")
        if tools:
            if not self.supports_tool_calls():
                # 降级处理：将工具定义注入 system prompt
                chat_req.pop("tools", None)
                chat_req.pop("tool_choice", None)
                chat_req = self._inject_tools_as_prompt(chat_req)

        return chat_req

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        """处理 Kimi 非流式响应"""
        choices = chat_resp.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            content = msg.get("content", "")

            # Kimi 旧版可能以 XML/JSON 格式返回 tool_calls
            if content and isinstance(content, str) and not msg.get("tool_calls"):
                extracted = self.extract_tool_calls_from_content(content)
                if extracted:
                    msg["tool_calls"] = extracted
                    msg["content"] = None

            # 确保 tool_calls 结构正确
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if "type" not in tc:
                    tc["type"] = "function"
                func = tc.get("function", {})
                if "arguments" in func and isinstance(func["arguments"], dict):
                    func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)

        return chat_resp

    def stream_event_transform(self, raw_event: dict) -> dict:
        """Kimi SSE 格式标准化"""
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
        """Kimi 新版支持 function calling，可通过配置控制"""
        return True

    def extract_tool_calls_from_content(self, content: str) -> list[dict] | None:
        """从 Kimi 旧版响应中提取 tool_calls"""
        if not content:
            return None

        # Kimi 旧版可能使用 <function_call> 或 JSON 格式
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
        """将 tool 定义注入为 system prompt 中的指令（降级策略）"""
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
