"""适配器基类 —— 定义国产模型适配器的抽象接口"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    """国产模型适配器基类

    子类必须设置:
      - name: 适配器名称 (如 "qwen", "deepseek")
      - base_url: 模型 API 基地址
      - api_key_env: API Key 对应的环境变量名
      - unsupported_features: 该提供商不支持的 Responses API 特性集合
    """

    name: str = ""
    base_url: str = ""
    api_key_env: str = ""

    # 该提供商不支持的特性，子类覆盖。可选值:
    #   "response_format" — 结构化 JSON 输出
    #   "tool_calls_parallel" — 并行多工具调用
    #   "logprobs" — token 概率
    #   "thinking" — 思维链推理
    unsupported_features: set[str] = set()

    # 适配器能力声明 —— 用于路由决策和能力发现
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

    # 思维链推理能力声明
    # supports_thinking_budget: 是否支持 budget_tokens 参数
    #   True: thinking: {type: "enabled", budget_tokens: N}
    #   False: thinking: {type: "enabled"} (仅开关，不含 budget_tokens)
    supports_thinking_budget: bool = True

    # thinking_mode: 控制思考模式的行为
    #   "auto" — 默认开启 thinking，通过 max_tokens 兜底
    #   "adaptive" — 自适应模式 (如 MiniMax M3)
    #   "off" — 默认关闭 thinking (如混元 HY3)
    #   "forced" — 强制开启，不可关闭 (如 Kimi K2.7 Code)
    thinking_mode: str = "auto"

    # thinking_effort 映射：统一 effort 到各厂商参数
    # 子类覆盖此方法来实现厂商特定的 thinking 控制
    def apply_thinking(self, chat_req: dict) -> dict:
        """统一的 thinking 控制入口 — 各适配器可按需覆盖"""
        return chat_req

    # ── 三个钩子方法 ─────────────────────────────────────────────

    def preprocess_chat_request(self, chat_req: dict) -> dict:
        """请求体微调 —— 在发送给上游模型之前调用

        可用于移除不支持的字段、调整参数格式等。
        """
        return chat_req

    def strip_unsupported(self, chat_req: dict) -> dict:
        """根据 unsupported_features 移除请求中不被支持的字段

        会在 translate_request() 生成 Chat 请求后、adapter 预处理前调用。
        """
        import logging
        _log = logging.getLogger("code-cn-bridge")
        unsupported = self.unsupported_features

        if "response_format" in unsupported and "response_format" in chat_req:
            _log.warning("模型 %s 不支持 structured output (response_format)，已移除", self.name)
            chat_req.pop("response_format", None)

        if "tool_calls_parallel" in unsupported and "tool_choice" in chat_req:
            tc = chat_req["tool_choice"]
            if tc == "auto":
                _log.warning("模型 %s 不支持并行工具调用，已禁用 tool_choice", self.name)
                chat_req.pop("tool_choice", None)

        return chat_req

    def postprocess_chat_response(self, chat_resp: dict) -> dict:
        """非流式响应微调 —— 在协议转换之前调用

        可用于修复字段结构、提取异常位置的 tool_calls 等。
        """
        return chat_resp

    def stream_event_transform(self, raw_event: dict) -> dict:
        """单个 SSE chunk 结构调整 —— 在流式转换前调用

        不同模型返回的 SSE 事件结构可能不同，
        此方法负责统一为标准的 Chat Completions chunk 格式。

        标准格式应为:
          {"choices": [{"index": 0, "delta": {...}, "finish_reason": ...}]}
        """
        return raw_event

    # ── 工具调用相关 ─────────────────────────────────────────────

    def supports_tool_calls(self) -> bool:
        """是否原生支持 function calling"""
        return True

    def extract_tool_calls_from_content(self, content: str) -> list[dict] | None:
        """尝试从 message.content 文本中提取 tool_calls"""
        return None

    # ── 工具方法 ─────────────────────────────────────────────────

    def get_headers(self, api_key: str) -> dict:
        """构建请求头（子类可覆盖以适配不同认证方式）"""
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def build_chat_url(self) -> str:
        """构建 Chat Completions API URL"""
        base = self.base_url.rstrip("/")
        return f"{base}/chat/completions"

    def build_image_gen_url(self) -> str:
        """构建 Image Generation API URL（DALL-E 兼容格式）"""
        base = self.base_url.rstrip("/")
        return f"{base}/images/generations"

    def preprocess_image_gen_request(self, req: dict) -> dict:
        """生图请求预处理 —— 子类可覆盖以适配不同生图 API 格式"""
        return req
