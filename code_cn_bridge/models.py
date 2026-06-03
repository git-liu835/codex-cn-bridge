"""Pydantic 数据模型 —— 定义 Responses API 和 Chat Completions API 的结构"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ── Chat Completions API 模型 ──────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatFunctionDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ChatToolDef(BaseModel):
    type: Literal["function"] = "function"
    function: ChatFunctionDef


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict | None = None


# ── Responses API 模型 ─────────────────────────────────────────────

class ResponsesInputItem(BaseModel):
    role: str
    content: str | list[dict] | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ResponsesRequest(BaseModel):
    model: str
    input: list[dict[str, Any]] = Field(default_factory=list)
    instructions: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict | None = "auto"
    previous_response_id: str | None = None
    conversation: str | dict | None = None
    reasoning: dict[str, Any] | None = None
    text: dict[str, Any] | None = None
    truncation: str | None = None
    metadata: dict[str, Any] | None = None


# ── Responses API 输出项 ───────────────────────────────────────────

def make_output_text(text: str) -> dict:
    return {"type": "output_text", "text": text, "annotations": []}


def make_message_output_item(content_text: str) -> dict:
    item_id = _uid("msg")
    return {
        "id": item_id,
        "object": "realtime.item",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [make_output_text(content_text)],
    }


def make_function_call_output_item(name: str, arguments: str, call_id: str | None = None) -> dict:
    fc_id = call_id or _uid("call")
    item_id = _uid("func")
    return {
        "id": item_id,
        "object": "realtime.item",
        "type": "function_call",
        "name": name,
        "call_id": fc_id,
        "arguments": arguments,
        "status": "completed",
    }


# ── Responses API 非流式响应 ───────────────────────────────────────

def build_responses_response(
    output_items: list[dict],
    model: str,
    usage: dict | None = None,
) -> dict:
    return {
        "id": _uid("resp"),
        "object": "response",
        "status": "completed",
        "model": model,
        "output": output_items,
        "usage": usage or {},
    }


# ── 错误响应 ───────────────────────────────────────────────────────

def build_error_response(message: str, code: str = "internal_error", status_code: int = 500) -> dict:
    return {
        "error": {
            "message": message,
            "type": code,
            "code": status_code,
        },
    }


def normalize_upstream_error(raw_body: str, status_code: int = 502) -> str:
    """将上游各种错误格式统一提取为人类可读的错误信息。

    上游 API 返回的错误格式五花八门：
    - OpenAI:    {"error": {"message": "..."}}
    - MiniMax:   {"base_resp": {"status_code": ..., "status_msg": "..."}}
    - DeepSeek:  {"error": {"message": "..."}}
    - 纯文本/HTML: 直接返回
    - JSON 但格式未知: 尝试提取常见字段

    返回: 标准化后的错误描述字符串
    """
    if not raw_body or not raw_body.strip():
        return f"Upstream {status_code}: (empty response body)"

    # 尝试解析 JSON
    try:
        import json
        data = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        # 非 JSON 返回（纯文本或 HTML），截取前 500 字符
        text = raw_body[:500].strip().replace("\n", " ").replace("\r", "")
        if len(raw_body) > 500:
            text += "..."
        return f"Upstream {status_code}: {text}"

    if not isinstance(data, dict):
        return f"Upstream {status_code}: {str(data)[:300]}"

    # OpenAI / DeepSeek 标准格式
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        msg = error_obj.get("message", "")
        err_type = error_obj.get("type", "")
        err_code = error_obj.get("code", "")
        if msg:
            parts = [f"Upstream {status_code}: {msg}"]
            if err_type:
                parts.append(f"(type={err_type}")
                if err_code:
                    parts[-1] += f", code={err_code}"
                parts[-1] += ")"
            return " ".join(parts)

    # MiniMax / 百度等自定义格式
    base_resp = data.get("base_resp")
    if isinstance(base_resp, dict):
        msg = base_resp.get("status_msg", "") or base_resp.get("message", "")
        code = base_resp.get("status_code", "")
        if msg:
            return f"Upstream {status_code}: {msg} (code={code})"

    # 尝试其他常见字段
    for key in ("msg", "message", "status_msg", "detail"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return f"Upstream {status_code}: {val}"

    # 兜底：序列化整个 JSON（短版）
    compact = json.dumps(data, ensure_ascii=False)[:300]
    return f"Upstream {status_code}: {compact}"
