"""协议转换引擎 —— OpenAI Responses API ↔ Chat Completions API 双向转换

包括：
- 请求转换 (Responses → Chat)
- 非流式响应转换 (Chat → Responses)
- 流式 SSE 转换 (Chat SSE → Responses SSE)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
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
# 会话粘性缓存 — 同一会话路由到同一 provider (ccx trace affinity)
# ═══════════════════════════════════════════════════════════════════
import time as _time

_AFFINITY_TTL = 600  # 会话粘性有效期: 10 分钟
_affinity_cache: dict[str, tuple[str, float]] = {}  # conv_id → (provider_name, expiry)


def set_affinity(conversation_id: str, provider_name: str) -> None:
    """记录会话粘性映射"""
    if conversation_id and provider_name:
        _affinity_cache[conversation_id] = (provider_name, _time.time() + _AFFINITY_TTL)
        _logger.debug("会话粘性: %s → %s (TTL %ds)", conversation_id, provider_name, _AFFINITY_TTL)


def get_affinity(conversation_id: str) -> str | None:
    """查询会话粘性，返回 provider_name 或 None"""
    if not conversation_id:
        return None
    entry = _affinity_cache.get(conversation_id)
    if entry:
        provider, expiry = entry
        if _time.time() < expiry:
            _logger.debug("会话粘性命中: %s → %s", conversation_id, provider)
            return provider
        del _affinity_cache[conversation_id]
    return None


def _extract_conversation_id(body: dict) -> str:
    """从 Responses API 请求体中提取会话标识符"""
    conv = body.get("conversation")
    if isinstance(conv, dict):
        cid = conv.get("id", "").strip()
        if cid:
            return cid
    return body.get("previous_response_id", "").strip()


# ═══════════════════════════════════════════════════════════════════
# reasoning_content 缓存 — 恢复 DeepSeek 丢弃的推理内容
# ═══════════════════════════════════════════════════════════════════
# DeepSeek 等模型在 tool_call 场景下会丢弃 assistant 消息中的
# reasoning_content。缓存上一条响应的 reasoning，在构建下一轮消息时
# 自动恢复到缺少 reasoning_content 的 assistant(tool_calls) 消息中。
_last_reasoning_content: str = ""


def save_last_reasoning(reasoning: str) -> None:
    """保存最近一条 assistant 消息的 reasoning_content，供下一轮恢复"""
    global _last_reasoning_content
    if reasoning:
        _last_reasoning_content = reasoning
        _logger.debug("已缓存 reasoning_content (%d 字符)", len(reasoning))


def _recover_reasoning(messages: list[dict]) -> None:
    """为缺少 reasoning_content 的 assistant(tool_calls) 消息恢复推理内容"""
    global _last_reasoning_content
    if not _last_reasoning_content:
        return
    recovered = False
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            if not msg.get("reasoning_content"):
                msg["reasoning_content"] = _last_reasoning_content
                recovered = True
    if recovered:
        _logger.info("已为 assistant(tool_calls) 消息恢复 reasoning_content (%d 字符)",
            len(_last_reasoning_content))


# ═══════════════════════════════════════════════════════════════════
# 响应缓存 — 支持 previous_response_id / conversation 上下文压缩
# ═══════════════════════════════════════════════════════════════════

import logging
import threading
from collections import OrderedDict

_logger = logging.getLogger("code-cn-bridge")


def _get_cache_dir() -> Path:
    """获取响应缓存目录，自动创建"""
    p = Path.home() / ".code-cn-bridge" / "cache" / "responses"
    p.mkdir(parents=True, exist_ok=True)
    return p


class ResponseCache:
    """LRU 缓存：存储最近 N 个响应的 output，供 previous_response_id 查询

    支持磁盘持久化，bridge 重启后自动恢复缓存，跨会话保留对话记忆。
    """

    def __init__(self, max_size: int = 500):
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.RLock()
        self._cache_dir = _get_cache_dir()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """从磁盘恢复最近的缓存条目"""
        try:
            if not self._cache_dir.exists():
                return
            files = sorted(
                self._cache_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            loaded = 0
            for fpath in files[:self._max_size]:
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    resp_id = data.get("id", fpath.stem)
                    self._cache[resp_id] = data
                    loaded += 1
                except Exception:
                    fpath.unlink(missing_ok=True)
            if loaded > 0:
                _logger.info("ResponseCache: 从磁盘恢复 %d 条缓存 (共 %d 条)", loaded, len(files))
        except Exception:
            pass

    def put(self, response_id: str, response_data: dict) -> None:
        with self._lock:
            if response_id in self._cache:
                self._cache.move_to_end(response_id)
            self._cache[response_id] = response_data
            while len(self._cache) > self._max_size:
                evict_id, _ = self._cache.popitem(last=False)
                self._delete_from_disk(evict_id)
            # 异步写入磁盘（不阻塞主流程）
            self._save_to_disk(response_id, response_data)

    def _save_to_disk(self, response_id: str, data: dict) -> None:
        try:
            fpath = self._cache_dir / f"{response_id}.json"
            tmp = fpath.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(fpath)
        except Exception:
            pass

    def _delete_from_disk(self, response_id: str) -> None:
        try:
            (self._cache_dir / f"{response_id}.json").unlink(missing_ok=True)
        except Exception:
            pass

    def get(self, response_id: str) -> dict | None:
        with self._lock:
            if response_id in self._cache:
                return self._cache.get(response_id)
        # 内存未命中，尝试磁盘回退
        return self._load_single_from_disk(response_id)

    def _load_single_from_disk(self, response_id: str) -> dict | None:
        fpath = self._cache_dir / f"{response_id}.json"
        if not fpath.exists():
            return None
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            # 加载到内存缓存
            with self._lock:
                if response_id not in self._cache:
                    self._cache[response_id] = data
                    while len(self._cache) > self._max_size:
                        self._cache.popitem(last=False)
            return data
        except Exception:
            return None

    def find_tool_call(self, call_id: str) -> dict | None:
        """在所有缓存的响应中查找 function_call 项

        用于续接对话时恢复缺失的 function_call 定义。
        """
        with self._lock:
            for resp_data in reversed(list(self._cache.values())):
                for out_item in resp_data.get("output", []):
                    if out_item.get("type") == "function_call":
                        tc_id = out_item.get("call_id", "") or out_item.get("id", "")
                        if tc_id == call_id:
                            return {
                                "type": "function",
                                "id": tc_id,
                                "function": {
                                    "name": out_item.get("name", ""),
                                    "arguments": out_item.get("arguments", ""),
                                },
                            }
        return None

    # 摘要大小限制，防止超大响应撑爆后续请求的上下文窗口
    MAX_ITEM_CHARS = 3000   # 单个 output item 文本上限
    MAX_SUMMARY_CHARS = 12000  # 摘要总长度上限

    def get_summary(self, response_id: str) -> str:
        """提取响应的文本摘要，用于注入后续请求的上下文

        对每个 output item 的文本做截断，并对总长度做硬限制，
        避免大响应（如长代码生成）导致第二次请求上下文溢出被截断。
        """
        resp = self.get(response_id)
        if not resp:
            return ""
        output = resp.get("output", [])
        parts: list[str] = []
        total_chars = 0

        for item in output:
            if total_chars >= self.MAX_SUMMARY_CHARS:
                break
            text = ""
            if item.get("type") == "message":
                for c in item.get("content", []):
                    t = c.get("text", "")
                    if t:
                        text += t
            elif item.get("type") == "reasoning":
                for c in item.get("content", []):
                    t = c.get("text", "")
                    if t:
                        text = f"[思考] {t}"
            elif item.get("type") == "function_call":
                name = item.get("name", "")
                args = item.get("arguments", "")
                text = f"[工具调用 {name}]: {args}"
            elif item.get("type") == "function_call_output":
                output_text = item.get("output", "")
                text = f"[工具结果]: {output_text}"

            if not text:
                continue

            # 单个 item 截断
            if len(text) > self.MAX_ITEM_CHARS:
                text = text[:self.MAX_ITEM_CHARS] + f"...[截断，原长 {len(text)} 字符]"

            remaining = self.MAX_SUMMARY_CHARS - total_chars
            if len(text) > remaining:
                text = text[:remaining] + f"...[摘要总长已达上限 {self.MAX_SUMMARY_CHARS} 字符]"

            parts.append(text)
            total_chars += len(text)

        result = "\n".join(parts)
        if total_chars > 0:
            _logger.info(
                "previous_response_id 摘要: response_id=%s, items=%d→%d, chars=%d",
                response_id, len(output), len(parts), total_chars,
            )
        return result


_response_cache: ResponseCache | None = None


def get_response_cache() -> ResponseCache:
    global _response_cache
    if _response_cache is None:
        from .config import get_config
        cfg = get_config()
        _response_cache = ResponseCache(max_size=cfg.response_cache_size)
    return _response_cache


# ═══════════════════════════════════════════════════════════════════
# 请求转换: Responses API → Chat Completions API
# ═══════════════════════════════════════════════════════════════════

def translate_request(
    responses_body: dict,
    adapter: BaseAdapter,
    target_model: str,
    alias: str = "",
) -> dict:
    """将 Responses API 请求转换为 Chat Completions API 请求

    完整支持 v2.0 所有 Responses API 特性:
    - previous_response_id / conversation → 上下文注入
    - reasoning.effort → thinking budget 映射
    - text.format → response_format (结构化输出)
    - truncation → 消息自动截断
    """
    messages = _map_input_to_messages(responses_body.get("input", []))

    # ── previous_response_id / conversation 上下文压缩 ──────────
    cached_summaries: list[str] = []

    prev_id = responses_body.get("previous_response_id", "").strip()
    if prev_id:
        cache = get_response_cache()
        summary = cache.get_summary(prev_id)
        if summary:
            cached_summaries.append(f"[前一轮对话摘要]\n{summary}")

    conversation = responses_body.get("conversation")
    if isinstance(conversation, dict):
        conv_id = conversation.get("id", "").strip()
        if conv_id and conv_id != prev_id:
            cache = get_response_cache()
            summary = cache.get_summary(conv_id)
            if summary:
                cached_summaries.append(f"[对话历史摘要: {conv_id}]\n{summary}")

    # instructions → system 消息
    instructions = responses_body.get("instructions", "").strip()

    # 从 config 注入持久化项目上下文（如 CLAUDE.md）
    from .config import get_config
    project_ctx = get_config().project_context
    if project_ctx and project_ctx not in instructions:
        if instructions:
            instructions = project_ctx + "\n\n---\n" + instructions
        else:
            instructions = project_ctx

    if instructions:
        msg_content = instructions
        if cached_summaries:
            msg_content += "\n\n---\n" + "\n\n".join(cached_summaries)
        messages.insert(0, {"role": "system", "content": msg_content})
    elif cached_summaries:
        messages.insert(0, {"role": "system", "content": "\n\n".join(cached_summaries)})

    chat_req: dict = {
        "model": target_model,
        "messages": messages,
        "stream": responses_body.get("stream", False),
    }

    # ── 可选参数映射 ────────────────────────────────────────────
    _map_optional(responses_body, chat_req, "temperature")
    _map_optional(responses_body, chat_req, "top_p")
    _map_optional(responses_body, chat_req, "stop")

    # max_output_tokens → max_tokens
    if "max_output_tokens" in responses_body:
        chat_req["max_tokens"] = responses_body["max_output_tokens"]

    # ── reasoning 参数映射 ──────────────────────────────────────
    # Responses API: reasoning: {effort: "low"|"medium"|"high", summary: "auto"}
    # Chat Completions: thinking: {type: "enabled", budget_tokens: N}
    # 从 model config 传入的 _disable_thinking（优先级最高）
    if responses_body.get("_disable_thinking"):
        chat_req["_disable_thinking"] = True

    reasoning = responses_body.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort", "")
        summary = reasoning.get("summary", "auto")
        budget_map = {"low": 1024, "medium": 4096, "high": 16384}
        chat_req["_thinking_budget"] = budget_map.get(effort, 4096)
        if summary == "none":
            chat_req["_disable_thinking"] = True
    else:
        chat_req.setdefault("_thinking_budget", 4096)

    # ── text.format → response_format (结构化输出) ────────────
    # Responses API: text: {format: {type: "json_schema", name: "...", schema: {...}}}
    # Chat Completions: response_format: {type: "json_schema", json_schema: {...}}
    text_cfg = responses_body.get("text")
    if isinstance(text_cfg, dict):
        fmt = text_cfg.get("format")
        if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
            schema = fmt.get("schema", {})
            name = fmt.get("name", "response_schema")
            strict = fmt.get("strict", True)
            chat_req["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "schema": schema,
                    "strict": strict,
                },
            }

    # ── 对话压缩 + 截断（保留记忆）─────────────────────────────
    # 两步策略：
    #   1. 超过 20 条非 system 消息 → 压缩最旧的消息为摘要
    #   2. 超过 30 条 → 额外截断（以防压缩后仍超限）
    # DeepSeek 实测：prompt_tokens > 98K 时只输出 3 token 就 stop。
    # 因此保守设为 64K，既能记住更长的上下文，又远离 98K 危险区。
    truncation = responses_body.get("truncation")
    max_tokens = chat_req.get("max_tokens", 4096)
    MAX_USER_TOKENS = 65536
    COMPRESS_THRESHOLD = 40  # 超过此数开始压缩旧消息
    TRUNCATE_THRESHOLD = 60  # 超过此数额外截断

    msgs = chat_req["messages"]
    msgs = _merge_system_messages(msgs)
    chat_req["messages"] = msgs
    system_msgs = [m for m in msgs if m.get("role") == "system"]
    other_msgs = [m for m in msgs if m.get("role") != "system"]

    # 第一步：压缩旧消息为摘要（保留记忆）
    if len(other_msgs) > COMPRESS_THRESHOLD:
        keep_recent = max(10, COMPRESS_THRESHOLD // 2)
        chat_req["messages"] = _compress_messages(msgs, keep_recent=keep_recent)
        other_msgs = [m for m in chat_req["messages"] if m.get("role") != "system"]
        _logger.info(
            "对话压缩: total=%d → compressed (kept %d recent + summary)",
            len(msgs), keep_recent,
        )

    # 第二步：如仍超限，token 截断
    if len(other_msgs) > TRUNCATE_THRESHOLD:
        system_msgs = [m for m in chat_req["messages"] if m.get("role") == "system"]
        other_msgs = [m for m in chat_req["messages"] if m.get("role") != "system"]
        _logger.warning(
            "消息过长自动截断: total=%d system=%d other=%d",
            len(chat_req["messages"]), len(system_msgs), len(other_msgs),
        )
        chat_req["messages"] = system_msgs + _truncate_messages(
            other_msgs,
            max_output_tokens=max_tokens,
            max_context_tokens=MAX_USER_TOKENS,
            preserve_system=False,
        )

    if truncation == "auto":
        chat_req["messages"] = _truncate_messages(
            chat_req["messages"],
            max_output_tokens=max_tokens,
        )

    # ── tools 映射 ──────────────────────────────────────────────
    tools = responses_body.get("tools")
    has_image_gen = False
    if tools:
        normalized = []
        for t in tools:
            tool_type = t.get("type", "function")
            if tool_type == "image_gen":
                has_image_gen = True
                normalized.append(_make_image_gen_tool(t))
            else:
                normalized.append(_normalize_tool(t))
        chat_req["tools"] = [t for t in normalized if t.get("function", {}).get("name", "").strip()]
        if has_image_gen:
            chat_req["_has_image_gen"] = True

    # tool_choice — 支持 "auto"、"none"、"required" 和精确指定
    tool_choice = responses_body.get("tool_choice")
    if tool_choice and tools:
        chat_req["tool_choice"] = tool_choice

    # ── 适配器降级处理 ─────────────────────────────────────────
    chat_req = adapter.strip_unsupported(chat_req)
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
    known_tc_ids: set = set()  # 所有已知 tool_call ID（包括已 flush 的）

    def _recover_tool_call_from_cache(call_id: str) -> dict | None:
        """从 ResponseCache 恢复缺失的 function_call 信息

        当 Codex 通过 conversation.id 续接对话时，input 中只有
        function_call_output 而没有对应的 function_call 项。
        此时需要从缓存中找到原始的 function_call 定义。
        """
        return get_response_cache().find_tool_call(call_id)

    def _ensure_tool_call_parent(call_id: str) -> None:
        """确保 tool 消息前有对应的 assistant(tool_calls) 消息

        处理续接对话时 function_call 项缺失的场景。
        如果 call_id 已在 pending_tool_calls 中或已在已知 ID 集合中，跳过。
        否则尝试从 ResponseCache 恢复缺失的 function_call 定义。
        """
        nonlocal pending_reasoning
        if not call_id:
            return
        if call_id in known_tc_ids:
            return
        # 检查是否在 pending 中
        for tc in pending_tool_calls:
            if tc.get("id") == call_id:
                known_tc_ids.add(call_id)
                return
        # 检查 messages 中是否已有包含此 call_id 的 assistant 消息
        for m in messages:
            if m.get("role") == "assistant":
                for tc in (m.get("tool_calls") or []):
                    if tc.get("id") == call_id:
                        known_tc_ids.add(call_id)
                        return
        # 尝试从 cache 恢复
        recovered = _recover_tool_call_from_cache(call_id)
        if recovered:
            known_tc_ids.add(call_id)
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [recovered],
            }
            msg["reasoning_content"] = "Previous tool call."
            messages.append(msg)
            _logger.info("从缓存恢复了缺失的 assistant(tool_calls) 消息: call_id=%s name=%s",
                call_id, recovered.get("function", {}).get("name", ""))
            return
        # 无法恢复，创建一个最小占位 assistant 消息（避免 400 错误）
        known_tc_ids.add(call_id)
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "type": "function",
                "id": call_id,
                "function": {"name": "unknown", "arguments": "{}"},
            }],
        }
        msg["reasoning_content"] = "Tool calls."
        messages.append(msg)
        _logger.warning("无法从缓存恢复 tool_call=%s，使用占位 assistant 消息", call_id)

    def _flush_tool_calls(flush_all: bool = False):
        """提交收集中的 tool_calls，附带 reasoning_content

        当 flush_all=True 时（首次 function_call_output 到达），将所有收集中的
        tool_calls 合并为一条 assistant 消息。这对应 codex-relay 的
        "parallel tool calls merged into one assistant message" 行为，
        确保多工具并行调用时 Chat API 收到正确的消息结构。

        当 flush_all=False 时，只提交已有对应 function_call_output 的 tool_calls，
        防止创建没有 tool message 跟随的 assistant 消息导致上游 400 错误。
        """
        nonlocal pending_reasoning
        if pending_tool_calls:
            if flush_all:
                to_flush = list(pending_tool_calls)
                pending_tool_calls.clear()
            else:
                to_flush = [tc for tc in pending_tool_calls if tc["id"] in responded_call_ids]
                pending_tool_calls[:] = [tc for tc in pending_tool_calls if tc["id"] not in responded_call_ids]
            if to_flush:
                for tc in to_flush:
                    known_tc_ids.add(tc.get("id", ""))
                msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": to_flush,
                }
                # Kimi thinking 模型要求所有带 tool_calls 的 assistant 消息必须有 reasoning_content
                msg["reasoning_content"] = pending_reasoning or "Tool calls."
                pending_reasoning = ""
                messages.append(msg)

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
            # 确保有对应的 assistant(tool_calls) 消息（续接对话时可能缺失）
            _ensure_tool_call_parent(call_id)
            # 并行工具调用合并：首次收到 tool 结果时，将所有收集中的
            # tool_calls 合并为一条 assistant 消息（codex-relay 做法）
            _flush_tool_calls(flush_all=True)

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
            for tc in item["tool_calls"]:
                known_tc_ids.add(tc.get("id", ""))
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

    # 恢复上一轮被 DeepSeek 丢弃的 reasoning_content
    _recover_reasoning(messages)

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
# truncation 辅助 — 上下文截断
# ═══════════════════════════════════════════════════════════════════

def _estimate_tokens(text: str) -> int:
    """简单 token 估算（中文 ~1.5 字/token，英文 ~4 字/token）"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


def _msg_token_count(msg: dict) -> int:
    """估算单条消息的 token 数"""
    total = 4  # role + 格式开销
    content = msg.get("content", "")
    if isinstance(content, str):
        total += _estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            total += _estimate_tokens(part.get("text", ""))
    for tc in msg.get("tool_calls", []):
        fn = tc.get("function", {})
        total += _estimate_tokens(fn.get("name", ""))
        total += _estimate_tokens(fn.get("arguments", ""))
    total += _estimate_tokens(msg.get("tool_call_id", ""))
    total += _estimate_tokens(msg.get("reasoning_content", ""))
    return total


def _get_max_context() -> int:
    from .config import get_config
    return get_config().max_context_tokens


def _merge_system_messages(messages: list[dict]) -> list[dict]:
    """将多个 system 消息合并为一条，防止非首位 system 消息被上游拒绝。

    MiniMax、Kimi 等严格校验的 provider 会在非首条 system 消息时报错。
    合并策略：保留第一条 system 消息的位置，将所有 system 消息的 content
    用换行连接，其他 role 的消息保持原位不变。
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    if len(system_msgs) <= 1:
        return messages

    # 收集所有 system 消息的文本内容
    parts = []
    for m in system_msgs:
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            )
        content = (content or "").strip()
        if content:
            parts.append(content)

    merged_content = "\n\n".join(parts)
    merged = {"role": "system", "content": merged_content}

    # 重建消息列表：找到第一条 system 消息的位置，替换之，跳过其余
    result = []
    found_first_system = False
    for m in messages:
        if m.get("role") == "system":
            if not found_first_system:
                result.append(merged)
                found_first_system = True
            # 跳过后续 system 消息
        else:
            result.append(m)

    _logger.info(
        "System 消息合并: %d 条 → 1 条 (%d 字符)",
        len(system_msgs), len(merged_content),
    )
    return result


def _compress_messages(messages: list[dict], keep_recent: int = 10) -> list[dict]:
    """压缩旧消息为结构化摘要，保留最近消息完整

    解决"聊得越久记忆丢失越多"的问题：
    - 保留最近 keep_recent 条非 system 消息原样
    - 更早的消息压缩为一个摘要 system 消息
    - 保护 tool_call/tool_result 配对：不会拆散配对消息
    - system 消息（提示词）始终原样保留在开头
    """
    MAX_CONTENT_LEN = 300  # 单条摘要内容上限
    MAX_SUMMARY_ITEMS = 25  # 最多保留 25 条旧消息的摘要

    system = [m for m in messages if m.get("role") == "system"]
    other = [m for m in messages if m.get("role") != "system"]

    if len(other) <= keep_recent:
        return messages

    # ── 保护 tool 配对：向前扫描确保 tool 消息有对应的 assistant(tool_calls) ──
    split_idx = len(other) - keep_recent
    while True:
        # 收集 recent 中 tool 消息引用的 call_id
        recent_tool_ids: set[str] = set()
        for m in other[split_idx:]:
            if m.get("role") == "tool":
                tid = m.get("tool_call_id", "")
                if tid:
                    recent_tool_ids.add(tid)

        if not recent_tool_ids:
            break

        # 收集 old 中 assistant(tool_calls) 覆盖的 call_id
        old_tc_ids: set[str] = set()
        for m in other[:split_idx]:
            if m.get("role") == "assistant":
                for tc in (m.get("tool_calls") or []):
                    old_tc_ids.add(tc.get("id", ""))

        # 找出需要保留的 call_id：出现在 recent tool 中，且父 assistant 在 old 中
        needs_parent = recent_tool_ids & old_tc_ids
        if not needs_parent:
            # recent 中的 tool 要么在 recent 中已有父 assistant，要么是真正的孤儿
            break

        # 向前扩展 split_idx，包含所有被 recent tool 引用的 assistant 消息
        found_any = False
        for i in range(split_idx - 1, -1, -1):
            m = other[i]
            if m.get("role") == "assistant":
                tc_ids = {tc.get("id", "") for tc in (m.get("tool_calls") or [])}
                if tc_ids & needs_parent:
                    split_idx = i
                    needs_parent -= tc_ids
                    found_any = True
                    if not needs_parent:
                        break
        if not found_any:
            break
        # 继续循环：扩展后新的 recent 可能引入更多需要配对的 tool 消息

    recent = other[split_idx:]
    old = other[:split_idx]

    _logger.info(
        "对话压缩保护配对: keep_recent=%d → 实际保留 %d 条 (向前扩展了 %d 条)",
        keep_recent, len(recent), len(recent) - keep_recent,
    )

    # 构建摘要
    parts: list[str] = []
    for m in old[-MAX_SUMMARY_ITEMS:]:
        role = m.get("role", "")
        content = m.get("content", "")

        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text")
            )
        content = (content or "").strip()

        # 工具调用 → 摘要
        tcs = m.get("tool_calls")
        if tcs:
            names = []
            for tc in tcs:
                fn = tc.get("function", {})
                names.append(fn.get("name", "unknown"))
            parts.append(f"[助手调用了工具: {', '.join(names)}]")
            continue

        # 工具结果 → 截取关键信息
        if role == "tool":
            tid = m.get("tool_call_id", "")[:8]
            short = content[:MAX_CONTENT_LEN]
            if len(content) > MAX_CONTENT_LEN:
                short += "..."
            parts.append(f"[工具结果 {tid}]: {short}")
            continue

        # 用户/助手消息
        if not content:
            continue
        label = "用户" if role == "user" else "助手"
        short = content[:MAX_CONTENT_LEN]
        if len(content) > MAX_CONTENT_LEN:
            short += "..."
        parts.append(f"[{label}]: {short}")

    if parts:
        summary = (
            "【对话历史摘要——以下为更早对话的压缩记录，你应记住其中关键信息】\n"
            + "\n".join(parts)
        )
        result = list(system)
        # 摘要放在最后一个 system 消息之后
        result.append({"role": "system", "content": summary})
        result.extend(recent)
        _logger.info(
            "对话压缩完成: %d 条旧消息 → 摘要 (%d 字符), 保留最近 %d 条",
            len(old), len(summary), len(recent),
        )
        return result

    return system + recent


def _truncate_messages(
    messages: list[dict],
    max_output_tokens: int = 4096,
    max_context_tokens: int | None = None,
    preserve_system: bool = True,
) -> list[dict]:
    """裁断消息列表以适配目标模型的上下文窗口

    保留策略：system 消息（索引 0）始终保留，然后从尾部保留尽可能多的消息。
    preserve_system=False 时不特殊处理 system 消息（调用方已自行处理）。
    """
    if max_context_tokens is None:
        from .config import get_config
        max_context_tokens = get_config().max_context_tokens

    available = max_context_tokens - max_output_tokens - 1024  # 1024 安全余量

    if not messages:
        return messages

    system_msg = None
    rest = messages
    if preserve_system and messages and messages[0].get("role") == "system":
        system_msg = messages[0]
        rest = messages[1:]

    system_tokens = _msg_token_count(system_msg) if system_msg else 0
    available -= system_tokens

    if available <= 0:
        return [system_msg] if system_msg else []

    # 从尾部向前累计，保留能放下的最后 N 条消息
    kept: list[dict] = []
    used = 0
    for m in reversed(rest):
        t = _msg_token_count(m)
        if used + t > available:
            break
        kept.append(m)
        used += t

    kept.reverse()

    # 确保 tool_call/tool_result 配对：孤儿 tool 消息会导致上游 400
    # 如果 kept 中有 tool 消息而其对应的 assistant(tool_calls) 被截掉了，
    # 向前扫描补回缺失的 assistant 消息及其后的 tool 消息
    truncation_idx = len(rest) - len(kept)
    while True:
        tc_ids_from_assistant: set[str] = set()
        orphan_ids: set[str] = set()
        for m in kept:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m.get("tool_calls", []):
                    tc_ids_from_assistant.add(tc.get("id", ""))
        for m in kept:
            if m.get("role") == "tool":
                tid = m.get("tool_call_id", "")
                if tid and tid not in tc_ids_from_assistant:
                    orphan_ids.add(tid)

        if not orphan_ids:
            break

        # 向前扫描找到覆盖孤儿 ID 的 assistant(tool_calls) 消息
        found = False
        extra: list[dict] = []
        for m in reversed(rest[:truncation_idx]):
            extra.append(m)
            if m.get("role") == "assistant" and m.get("tool_calls"):
                msg_tc_ids = {tc.get("id", "") for tc in m.get("tool_calls", [])}
                if msg_tc_ids & orphan_ids:
                    found = True
                    break
        if not found:
            break
        extra.reverse()
        kept = extra + kept
        truncation_idx -= len(extra)

    result = [system_msg] if system_msg else []
    result.extend(kept)
    return result


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def _normalize_usage(usage: dict) -> dict:
    """将 Chat Completions usage 转换为 Responses API 格式

    Chat Completions: {prompt_tokens, completion_tokens, total_tokens}
    Responses API:    {input_tokens, output_tokens, total_tokens}
    """
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


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
        reasoning_content = msg.get("reasoning_content", "")

        # 推理内容 → reasoning 输出项
        if reasoning_content:
            save_last_reasoning(reasoning_content)
            reasoning_id = _uid("reas")
            output_items.append({
                "id": reasoning_id,
                "object": "realtime.item",
                "type": "reasoning",
                "status": "completed",
                "content": [{"type": "summary_text", "text": reasoning_content}],
            })

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

    return build_responses_response(output_items, model, _normalize_usage(usage))


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

        # 推理追踪 (reasoning_content → reasoning item)
        self._reasoning_item_index = -1
        self._reasoning_item_id = ""
        self._reasoning_content_index = -1
        self._reasoning_buf: list[str] = []
        self._reasoning_started = False

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
        self._usage: dict = {}  # 从最后一个 chunk 捕获的 usage 信息

    # ── 入口 ─────────────────────────────────────────────────────

    def warmup(self) -> list[str]:
        """响应预热: 立即发送 response.created 事件，不等上游响应

        ccx 风格: 在发起上游请求前先发送 HTTP 状态码和初始 SSE 事件，
        减少 Codex 感知到的首字节延迟。
        """
        if not self._created_sent:
            return self._emit_created()
        return []

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

        # 捕获 usage 信息（通常出现在最后一个 chunk 中）
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = usage

        choices = chunk.get("choices", [])
        if not choices:
            _logger.debug("StreamTranslator: 空choices chunk (keys=%s)", list(chunk.keys()))
            return

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason") or ""
        if finish_reason:
            self._finish_reason = finish_reason

        # 推理增量 (reasoning_content → reasoning item)
        # 将模型的内部推理转换为 Responses API 的 reasoning 输出项，
        # 让 Codex 能正确展示和使用模型的思考过程
        reasoning = delta.get("reasoning_content")
        if reasoning:
            yield from self._handle_reasoning_delta(reasoning)

        # 当推理结束、实际内容开始时，先关闭推理项
        content = delta.get("content")
        if content:
            if self._reasoning_started:
                yield from self._emit_reasoning_done()
            yield from self._handle_text_delta(content)

        # 工具调用增量 — 如果之前在推理中，先结束推理
        tool_calls = delta.get("tool_calls", [])
        if tool_calls:
            if self._reasoning_started:
                yield from self._emit_reasoning_done()

        for tc in tool_calls:
            yield from self._handle_tool_call_delta(tc)

        # 完成
        if finish_reason:
            if self._reasoning_started:
                yield from self._emit_reasoning_done()
            yield from self._finish()

    def _finish(self) -> list[str]:
        """流结束时的收尾事件"""
        if self._done:
            return []
        events: list[str] = []

        # 结束推理项 (如果还在进行中)
        if self._reasoning_started:
            events.extend(self._emit_reasoning_done())

        # 结束文本项 (如果还在进行中)
        if self._text_started:
            events.extend(self._emit_text_done())

        # 结束工具调用项
        for idx in sorted(self._tc_buf.keys()):
            events.extend(self._emit_tool_call_done(idx))

        # ── 根据 finish_reason 决定 response 状态 ──────────────
        # "tool_calls" / "function_call" → Codex 需要执行工具
        # "stop" → 正常完成
        # "length" → token 截断
        # 其他 / 有错误 → 失败
        has_tool_calls = bool(self._tc_buf)
        if has_tool_calls:
            status = "requires_action"
        elif self._finish_reason in ("stop", ""):
            status = "completed"
        elif self._finish_reason == "length":
            status = "completed"  # 截断也算完成，让 Codex 自己处理
            _logger.warning(
                "StreamTranslator: token 截断! finish_reason=length, output_items=%d, usage=%s",
                len(self._output_items), self._usage,
            )
        else:
            status = "completed"

        # response.completed
        completed_event: dict = {
            "type": "response.completed",
            "response": {
                "id": self.response_id,
                "object": "response",
                "model": self.model,
                "status": status,
                "output": self._output_items,
            },
        }
        # 附加 usage 信息（转换为 Responses API 字段名）
        completed_event["response"]["usage"] = _normalize_usage(self._usage)

        events.append(_sse_line(completed_event))
        self._done = True
        _logger.debug("StreamTranslator: response.completed id=%s status=%s output_items=%d usage=%s",
            self.response_id, status, len(self._output_items), completed_event["response"].get("usage", {}))
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

    def _handle_reasoning_delta(self, reasoning: str) -> list[str]:
        """将 reasoning_content delta 转换为 Responses API 的 reasoning 输出项"""
        events = []
        if not self._reasoning_started:
            self._output_index += 1
            self._reasoning_item_index = self._output_index
            self._reasoning_item_id = _uid("reas")
            self._reasoning_content_index = 0
            self._reasoning_buf = []
            self._reasoning_started = True

            item = {
                "id": self._reasoning_item_id,
                "object": "realtime.item",
                "type": "reasoning",
                "status": "in_progress",
                "content": [],
            }
            self._output_items.append(item)

            # event: response.output_item.added
            events.append(
                _sse_line({
                    "type": "response.output_item.added",
                    "output_index": self._reasoning_item_index,
                    "item": item,
                })
            )

            # event: response.reasoning_summary_part.added
            part = {"type": "summary_text", "text": ""}
            item["content"].append(part)
            events.append(
                _sse_line({
                    "type": "response.reasoning_summary_part.added",
                    "output_index": self._reasoning_item_index,
                    "content_index": self._reasoning_content_index,
                    "part": part,
                })
            )

        self._reasoning_buf.append(reasoning)

        # event: response.reasoning_summary_text.delta
        events.append(
            _sse_line({
                "type": "response.reasoning_summary_text.delta",
                "output_index": self._reasoning_item_index,
                "content_index": self._reasoning_content_index,
                "delta": reasoning,
            })
        )
        return events

    def _emit_reasoning_done(self) -> list[str]:
        """结束推理输出项"""
        if not self._reasoning_started:
            return []
        events = []

        reasoning_text = "".join(self._reasoning_buf)
        if self._reasoning_item_index < len(self._output_items):
            item = self._output_items[self._reasoning_item_index]
            item["status"] = "completed"
            if item["content"]:
                item["content"][0]["text"] = reasoning_text

        # event: response.reasoning_summary_part.done
        events.append(
            _sse_line({
                "type": "response.reasoning_summary_part.done",
                "output_index": self._reasoning_item_index,
                "content_index": self._reasoning_content_index,
                "part": self._output_items[self._reasoning_item_index]["content"][0] if self._reasoning_item_index < len(self._output_items) else {},
            })
        )

        # event: response.output_item.done
        events.append(
            _sse_line({
                "type": "response.output_item.done",
                "output_index": self._reasoning_item_index,
                "item": self._output_items[self._reasoning_item_index] if self._reasoning_item_index < len(self._output_items) else {},
            })
        )
        self._reasoning_started = False
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

        # event: response.content_part.done — 必须发出，让 Codex 确认文本接收完毕
        events.append(
            _sse_line({
                "type": "response.content_part.done",
                "output_index": self._text_item_index,
                "content_index": self._text_content_index,
                "part": self._output_items[self._text_item_index]["content"][0] if self._text_item_index < len(self._output_items) else {},
            })
        )
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
        """处理工具调用增量 —— 延迟发送 output_item.added 直到参数开始到达

        ccx 风格懒加载：避免在工具调用名称出现时立即发射 output_item.added。
        推迟到第一个参数 delta 到达时才发送，防止 Codex 等待不会产生参数的
        "幻影"工具调用项。
        """
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
                "item_added_sent": False,  # 是否已发送 output_item.added
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

        # 名字事件（首次出现时）—— 仅记录，不发送 output_item.added
        if fn_name and not buf["name_done"]:
            buf["name"] = fn_name
            buf["name_done"] = True
            if buf["item_index"] < len(self._output_items):
                self._output_items[buf["item_index"]]["name"] = fn_name

        # 参数增量 —— 首次参数到达时才发送 output_item.added
        if fn_args:
            buf["arguments"] += fn_args
            if buf["item_index"] < len(self._output_items):
                self._output_items[buf["item_index"]]["arguments"] = buf["arguments"]

            if not buf["item_added_sent"]:
                buf["item_added_sent"] = True
                events.append(
                    _sse_line({
                        "type": "response.output_item.added",
                        "output_index": buf["item_index"],
                        "item": self._output_items[buf["item_index"]],
                    })
                )

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

        events = []
        # 如果 output_item.added 还没发送（工具调用有名称但无参数），补发
        if not buf.get("item_added_sent"):
            buf["item_added_sent"] = True
            events.append(
                _sse_line({
                    "type": "response.output_item.added",
                    "output_index": item_idx,
                    "item": self._output_items[item_idx] if item_idx < len(self._output_items) else {},
                })
            )
        # response.function_call_arguments.done — 必须发出，让 Codex 确认参数接收完毕
        events.append(
            _sse_line({
                "type": "response.function_call_arguments.done",
                "output_index": item_idx,
                "call_id": buf["call_id"],
                "arguments": buf["arguments"],
            })
        )
        # response.output_item.done
        events.append(
            _sse_line({
                "type": "response.output_item.done",
                "output_index": item_idx,
                "item": self._output_items[item_idx] if item_idx < len(self._output_items) else {},
            })
        )
        return events


def _sse_line(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
