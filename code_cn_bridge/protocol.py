"""协议转换引擎 —— OpenAI Responses API ↔ Chat Completions API 双向转换

包括：
- 请求转换 (Responses → Chat)
- 非流式响应转换 (Chat → Responses)
- 流式 SSE 转换 (Chat SSE → Responses SSE)
"""

from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

from .models import (
    _uid,
    build_responses_response,
    build_error_response,
    make_function_call_output_item,
    make_output_text,
    make_message_output_item,
)
from .adapters.base import BaseAdapter


# ═══════════════════════════════════════════════════════════════════
# 请求转换: Responses API → Chat Completions API
# ═══════════════════════════════════════════════════════════════════

def translate_request(
    responses_body: dict,
    adapter: BaseAdapter,
    target_model: str,
    alias: str = "",
) -> dict:
    """将 Responses API 请求转换为 Chat Completions API 请求"""
    messages = _map_input_to_messages(responses_body.get("input", []))

    # instructions → 前缀 system 消息（code 的系统提示词）
    instructions = responses_body.get("instructions", "").strip()
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})

    # 模型身份注入：让模型以配置的目标模型名自居
    if alias and alias != target_model:
        identity_msg = (
            f"You are {target_model}. "
            f"If asked what model or version you are, say you are {target_model}."
        )
        insert_at = 1 if instructions else 0
        messages.insert(insert_at, {"role": "system", "content": identity_msg})

    chat_req: dict = {
        "model": target_model,
        "messages": messages,
        "stream": responses_body.get("stream", False),
    }

    # 可选参数映射
    _map_optional(responses_body, chat_req, "temperature")
    _map_optional(responses_body, chat_req, "top_p")
    _map_optional(responses_body, chat_req, "stop")

    # max_output_tokens → max_tokens
    if "max_output_tokens" in responses_body:
        chat_req["max_tokens"] = responses_body["max_output_tokens"]

    # tools: 确保每个 tool 都有 type: "function"，过滤空名工具
    tools = responses_body.get("tools")
    has_image_gen = False
    if tools:
        normalized = []
        for t in tools:
            tool_type = t.get("type", "function")
            if tool_type == "image_gen":
                has_image_gen = True
                # 将 image_gen 内置工具转换为 function tool，让国产 LLM 能调用
                normalized.append(_make_image_gen_tool(t))
            else:
                normalized.append(_normalize_tool(t))
        chat_req["tools"] = [t for t in normalized if t.get("function", {}).get("name", "").strip()]
        if has_image_gen:
            chat_req["_has_image_gen"] = True

    # tool_choice
    tool_choice = responses_body.get("tool_choice")
    if tool_choice and tools:
        chat_req["tool_choice"] = tool_choice

    # 适配器预处理
    chat_req = adapter.preprocess_chat_request(chat_req)
    return chat_req


# 国产模型不支持的 role，映射到 system
_ROLE_MAP = {"developer": "system"}


def _extract_reasoning_text(item: dict) -> str:
    """从 reasoning 类型的 item 中提取文本"""
    parts = []
    for field in ("summary", "content"):
        for part in item.get(field, []) or []:
            text = part.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _map_input_to_messages(input_items: list[dict]) -> list[dict]:
    """将 Responses API 的 input 数组映射为 Chat 的 messages 数组"""
    messages = []
    pending_tool_calls: list[dict] = []  # 收集连续的 function_call
    pending_reasoning: str = ""  # 收集 reasoning 文本，附加到紧随的 assistant 消息
    responded_call_ids: set = set()  # 跟踪已有 function_call_output 响应的 call_id

    def _flush_tool_calls():
        """提交收集中的 tool_calls，附带 reasoning_content（Kimi 等 thinking 模型需要）

        只提交已收到对应 function_call_output 的 tool_calls，防止创建后面没有
        tool message 跟随的 assistant 消息导致上游 400 错误。
        """
        nonlocal pending_reasoning
        if pending_tool_calls:
            # 分离已解决和未解决的 tool_calls
            resolved = [tc for tc in pending_tool_calls if tc["id"] in responded_call_ids]
            if resolved:
                msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": resolved,
                }
                # Kimi thinking 模型要求所有带 tool_calls 的 assistant 消息必须有 reasoning_content
                msg["reasoning_content"] = pending_reasoning or "Tool calls."
                pending_reasoning = ""
                messages.append(msg)
            # 移除已解决的，保留未解决的等待后续 output
            pending_tool_calls[:] = [tc for tc in pending_tool_calls if tc["id"] not in responded_call_ids]

    for item in input_items:
        item_type = item.get("type", "")

        # reasoning → 收集文本，附加到下一个 assistant 消息
        if item_type == "reasoning":
            pending_reasoning = _extract_reasoning_text(item) or pending_reasoning
            continue

        # function_call_output → tool role (工具调用结果)
        if item_type == "function_call_output":
            call_id = item.get("call_id", "")
            responded_call_ids.add(call_id)
            _flush_tool_calls()

            # output 可能是字符串或结构化的 output_text 列表
            output = item.get("output", "")
            if isinstance(output, list):
                output = "".join(p.get("text", "") for p in output)
            elif not isinstance(output, str):
                output = str(output)

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": output,
            })
            continue

        # function_call → 收集到 pending（合并连续多个为一条 assistant 消息）
        if item_type == "function_call":
            tc = {
                "type": "function",
                "id": item.get("call_id", ""),
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                },
            }
            pending_tool_calls.append(tc)
            continue

        # 遇到非 function_call 的消息，先提交之前收集的 tool_calls
        _flush_tool_calls()

        role = item.get("role", "user")
        role = _ROLE_MAP.get(role, role)
        content = _normalize_content(item.get("content", ""))
        msg = {"role": role}
        if content is not None:
            msg["content"] = content or None
        if "name" in item:
            msg["name"] = item["name"]
        if "tool_call_id" in item:
            msg["tool_call_id"] = item["tool_call_id"]
        if "tool_calls" in item:
            msg["tool_calls"] = item["tool_calls"]
            if not msg.get("content"):
                msg["content"] = None

        # assistant 消息：附加之前收集的 reasoning_content
        if role == "assistant" and pending_reasoning:
            msg["reasoning_content"] = pending_reasoning
            pending_reasoning = ""

        messages.append(msg)

    # 末尾如果还有未提交的 tool_calls（_flush_tool_calls 内部会过滤未解决的）
    _flush_tool_calls()

    # 末尾如果还有未消费的 reasoning（极少情况，附加到最后一个 assistant 消息）
    if pending_reasoning:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                m["reasoning_content"] = pending_reasoning
                break
        pending_reasoning = ""

    return messages


def _normalize_content(content) -> str | list[dict] | None:
    """将 Responses API 的 content 格式转换为 Chat 格式

    Responses: [{"type": "input_text", "text": "Hello"}]
    Chat:      [{"type": "text", "text": "Hello"}]  或 纯字符串 "Hello"
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        has_text = False
        for part in content:
            ptype = part.get("type", "")
            # 映射 type 名
            if ptype == "input_text":
                parts.append({"type": "text", "text": part.get("text", "")})
                has_text = True
            elif ptype == "input_image":
                parts.append({"type": "image_url", "image_url": part.get("image_url", {})})
            elif ptype == "output_text":
                parts.append({"type": "text", "text": part.get("text", "")})
                has_text = True
            else:
                # 透传未知类型
                parts.append(part)
        # 如果只有一个纯文本，直接返回字符串
        if len(parts) == 1 and has_text:
            return parts[0]["text"]
        return parts if parts else None
    return content


def _make_image_gen_tool(tool: dict) -> dict:
    """将 code 内置 image_gen 工具转换为国产 LLM 可理解的 function tool"""
    return {
        "type": "function",
        "function": {
            "name": "image_gen",
            "description": "Generate photographic images, artwork, illustrations, UI mockups, and any visual/raster bitmap from a text prompt. Call this whenever the user asks to create, draw, generate, design, or visualize an image. The prompt should be a detailed, production-ready image generation specification.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "A detailed, structured image generation prompt describing exactly what to create, including subject, scene, style, composition, lighting, colors, and constraints. Write this as a complete production spec, not a casual description."
                    },
                    "size": {
                        "type": "string",
                        "enum": ["2560x1440", "2048x2048", "3840x2160", "4096x4096"],
                        "description": "Output image dimensions. Minimum 3686400 pixels required. Default 2560x1440 for landscape."
                    },
                },
                "required": ["prompt"]
            }
        }
    }


def _normalize_tool(tool: dict) -> dict:
    """确保 tool 格式为 {"type": "function", "function": {...}}"""
    if "type" not in tool:
        tool = {"type": "function", **tool}
    if "function" not in tool:
        tool["function"] = {
            "name": tool.pop("name", ""),
            "description": tool.pop("description", ""),
            "parameters": tool.pop("parameters", {}),
        }
        tool["type"] = "function"
    # 修复 parameters：必须是一个 type: "object" 的 JSON Schema
    params = tool["function"].get("parameters")
    if not params or not isinstance(params, dict):
        tool["function"]["parameters"] = {"type": "object", "properties": {}}
    elif params.get("type") != "object":
        params["type"] = "object"
        if "properties" not in params:
            params["properties"] = {}
    return tool


def _map_optional(src: dict, dst: dict, key: str) -> None:
    if key in src and src[key] is not None:
        dst[key] = src[key]


# ═══════════════════════════════════════════════════════════════════
# 非流式响应转换: Chat Completions API → Responses API
# ═══════════════════════════════════════════════════════════════════

def translate_response(
    chat_resp: dict,
    adapter: BaseAdapter,
    model: str,
) -> dict:
    """将 Chat Completions 响应转换为 Responses API 格式"""
    chat_resp = adapter.postprocess_chat_response(chat_resp)

    choices = chat_resp.get("choices", [])
    usage = chat_resp.get("usage", {})
    output_items: list[dict] = []

    for choice in choices:
        msg = choice.get("message", {})
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []

        # 文本内容
        if content:
            output_items.append(make_message_output_item(content))

        # 工具调用
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            arguments = fn.get("arguments", "")
            call_id = tc.get("id", "")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            output_items.append(
                make_function_call_output_item(name, arguments, call_id)
            )

    return build_responses_response(output_items, model, usage)


# ═══════════════════════════════════════════════════════════════════
# 流式转换: Chat Completions SSE → Responses API SSE
# ═══════════════════════════════════════════════════════════════════

class StreamTranslator:
    """有状态的流式转换器

    将 Chat Completions 的 SSE 流逐块转换为 Responses API 的 SSE 事件流。
    """

    def __init__(self, response_id: str | None = None, model: str = ""):
        self.response_id = response_id or _uid("resp")
        self.model = model

        # 状态
        self._created_sent = False
        self._done = False
        self._output_index = -1

        # 文本输出追踪
        self._text_item_index = -1
        self._text_item_id = ""
        self._text_content_index = -1
        self._text_buf: list[str] = []
        self._text_started = False

        # 工具调用缓冲 (按 index 分组)
        # {index: {"id": str, "name": str, "arguments": str, "item_index": int}}
        self._tc_buf: dict[int, dict] = {}

        # 最终输出列表
        self._output_items: list[dict] = []

        # 辅助
        self._accumulated_text = ""
        self._finish_reason = ""

    # ── 入口 ─────────────────────────────────────────────────────

    async def translate_stream(
        self,
        chat_stream: AsyncIterator[dict],
    ) -> AsyncIterator[str]:
        """将 Chat SSE stream 转换为 Responses SSE stream 的字符串行"""
        try:
            async for chunk in chat_stream:
                for event_line in self._process_chunk(chunk):
                    yield event_line
            for event_line in self._finish():
                yield event_line
        except Exception as exc:
            yield _sse_line(build_error_response(str(exc)))

    def translate_chunk(self, chunk: dict) -> list[str]:
        """同步版本：处理单个 chunk"""
        return list(self._process_chunk(chunk))

    # ── 核心处理逻辑 ─────────────────────────────────────────────

    def _process_chunk(self, chunk: dict):
        """处理单个 Chat SSE chunk，生成 Responses SSE 事件行"""
        if self._done:
            return

        if not self._created_sent:
            yield from self._emit_created()

        choices = chunk.get("choices", [])
        if not choices:
            return

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason") or ""
        if finish_reason:
            self._finish_reason = finish_reason

        # 文本增量 (忽略 reasoning_content，仅转发实际 content)
        # deepseek-v4-pro 等模型在思考阶段产生 reasoning_content，
        # 这些是模型内部推理，不应转发给 code，否则会造成循环
        content = delta.get("content")
        if content:
            yield from self._handle_text_delta(content)

        # 工具调用增量
        tool_calls = delta.get("tool_calls", [])
        for tc in tool_calls:
            yield from self._handle_tool_call_delta(tc)

        # 完成
        if finish_reason:
            yield from self._finish()

    def _finish(self) -> list[str]:
        """流结束时的收尾事件"""
        if self._done:
            return []
        events: list[str] = []

        # 结束文本项 (如果还在进行中)
        if self._text_started:
            events.extend(self._emit_text_done())

        # 结束工具调用项
        for idx in sorted(self._tc_buf.keys()):
            events.extend(self._emit_tool_call_done(idx))

        # response.completed
        events.append(
            _sse_line({
                "type": "response.completed",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "model": self.model,
                    "status": "completed",
                    "output": self._output_items,
                },
            })
        )
        self._done = True
        return events

    # ── 事件生成 ─────────────────────────────────────────────────

    def _emit_created(self):
        events = []
        events.append(
            _sse_line({
                "type": "response.created",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "model": self.model,
                    "status": "in_progress",
                    "output": [],
                },
            })
        )
        self._created_sent = True
        return events

    def _handle_text_delta(self, content: str) -> list[str]:
        events = []
        if not self._text_started:
            # 开始新的文本输出项
            self._output_index += 1
            self._text_item_index = self._output_index
            self._text_item_id = _uid("msg")
            self._text_content_index = 0
            self._text_buf = []
            self._text_started = True

            # 生成 output_item 和 content_part 的占位记录
            item = {
                "id": self._text_item_id,
                "object": "realtime.item",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
            self._output_items.append(item)

            # event: response.output_item.added
            events.append(
                _sse_line({
                    "type": "response.output_item.added",
                    "output_index": self._text_item_index,
                    "item": item,
                })
            )

            # event: response.content_part.added
            part = {"type": "output_text", "text": "", "annotations": []}
            item["content"].append(part)
            events.append(
                _sse_line({
                    "type": "response.content_part.added",
                    "output_index": self._text_item_index,
                    "content_index": self._text_content_index,
                    "part": part,
                })
            )

        self._text_buf.append(content)
        self._accumulated_text += content

        # event: response.output_text.delta
        events.append(
            _sse_line({
                "type": "response.output_text.delta",
                "output_index": self._text_item_index,
                "content_index": self._text_content_index,
                "delta": content,
            })
        )
        return events

    def _emit_text_done(self) -> list[str]:
        if not self._text_started:
            return []
        events = []

        # 更新 item 状态
        if self._text_item_index < len(self._output_items):
            item = self._output_items[self._text_item_index]
            item["status"] = "completed"
            if item["content"]:
                item["content"][0]["text"] = self._accumulated_text

        # event: response.output_item.done
        events.append(
            _sse_line({
                "type": "response.output_item.done",
                "output_index": self._text_item_index,
                "item": self._output_items[self._text_item_index] if self._text_item_index < len(self._output_items) else {},
            })
        )
        self._text_started = False
        return events

    def _handle_tool_call_delta(self, tc: dict) -> list[str]:
        events = []
        tc_index = tc.get("index", 0)
        fn = tc.get("function", {})
        fn_name = fn.get("name", "")
        fn_args = fn.get("arguments", "")
        tc_id = tc.get("id", "")

        if tc_index not in self._tc_buf:
            # 新的工具调用
            self._output_index += 1
            item_id = tc_id or _uid("func")
            call_id = tc_id or _uid("call")

            self._tc_buf[tc_index] = {
                "id": item_id,
                "call_id": call_id,
                "name": "",
                "arguments": "",
                "item_index": self._output_index,
                "name_done": False,
            }

            # 占位 item
            item = {
                "id": item_id,
                "object": "realtime.item",
                "type": "function_call",
                "call_id": call_id,
                "name": "",
                "arguments": "",
                "status": "in_progress",
            }
            self._output_items.append(item)

        buf = self._tc_buf[tc_index]

        # 名字事件（首次出现时）
        if fn_name and not buf["name_done"]:
            buf["name"] = fn_name
            buf["name_done"] = True
            if buf["item_index"] < len(self._output_items):
                self._output_items[buf["item_index"]]["name"] = fn_name

            events.append(
                _sse_line({
                    "type": "response.output_item.added",
                    "output_index": buf["item_index"],
                    "item": self._output_items[buf["item_index"]],
                })
            )

        # 参数增量
        if fn_args:
            buf["arguments"] += fn_args
            if buf["item_index"] < len(self._output_items):
                self._output_items[buf["item_index"]]["arguments"] = buf["arguments"]

            events.append(
                _sse_line({
                    "type": "response.function_call_arguments.delta",
                    "output_index": buf["item_index"],
                    "call_id": buf["call_id"],
                    "delta": fn_args,
                })
            )

        return events

    def _emit_tool_call_done(self, tc_index: int) -> list[str]:
        buf = self._tc_buf[tc_index]
        item_idx = buf["item_index"]
        if item_idx < len(self._output_items):
            self._output_items[item_idx]["status"] = "completed"

        return [
            _sse_line({
                "type": "response.output_item.done",
                "output_index": item_idx,
                "item": self._output_items[item_idx] if item_idx < len(self._output_items) else {},
            })
        ]


def _sse_line(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
