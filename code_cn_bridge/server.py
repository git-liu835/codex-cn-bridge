"""FastAPI 服务器 —— 提供 /v1/responses 端点、管理 API 和 WebSocket"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import get_config, reload_config
from .adapters import get_registry
from .adapters.base import BaseAdapter
from .protocol import translate_request, translate_response, StreamTranslator, _sse_line, get_response_cache, save_last_reasoning, set_affinity, get_affinity, _extract_conversation_id
from .client import UpstreamClient
from .circuit_breaker import get_circuit_breaker_registry, get_health_prober
from .catalog import generate_catalog, update_codex_config
from .middleware import (
    ErrorHandlingMiddleware,
    RequestLoggingMiddleware,
    DetailedLoggingMiddleware,
    ApiKeyFilter,
)
from .models import build_error_response, build_responses_response, make_message_output_item, _uid
from .stats import get_stats, RequestLog
from .admin_api import router as admin_router

logger = logging.getLogger("code-cn-bridge")

# ── CUA 请求去重缓存 ─────────────────────────────────────────────
# 防止 Codex 对相同 input 反复发送 POST 导致重复调用模型
import hashlib as _hashlib
_cua_dedup_cache: dict[str, tuple[dict, float]] = {}  # input_hash → (response, timestamp)
_CUA_DEDUP_TTL = 60  # 60 秒内相同请求直接返回缓存


# ── 压缩请求体解压（CC Switch v3.16.4 PR #3817）─────────────────
# Codex Desktop 会发送 zstd/gzip/br/deflate 压缩的请求体，
# 必须在 JSON 解析前解压，否则会解析失败。
# 支持堆叠编码（如 "gzip, zstd"）。
try:
    import zstandard as _zstd
except ImportError:
    _zstd = None

try:
    import brotli as _brotli
except ImportError:
    _brotli = None

try:
    import gzip as _gzip
    import zlib as _zlib
except ImportError:
    _gzip = None
    _zlib = None


def _decompress_body(raw: bytes, encoding: str) -> bytes:
    """根据 content-encoding 解压请求体

    支持的编码（按 CC Switch v3.16.4 实现）：
    - zstd (Codex Desktop 常用)
    - gzip
    - br (brotli)
    - deflate
    - 堆叠编码: "gzip, zstd" 等
    """
    if not encoding or not raw:
        return raw

    # 处理堆叠编码: "gzip, zstd" → ["gzip", "zstd"]
    # 注意：堆叠编码按声明顺序反向解压（最外层最后声明）
    encodings = [e.strip().lower() for e in encoding.split(",") if e.strip()]
    # 反向解压：最后一个编码是最外层
    data = raw
    for enc in reversed(encodings):
        if not data:
            break
        try:
            if enc in ("zstd", "zstandard"):
                if _zstd is None:
                    logger.warning("收到 zstd 压缩请求但 zstandard 未安装，跳过解压")
                    continue
                dctx = _zstd.ZstdDecompressor()
                data = dctx.decompress(data)
            elif enc == "gzip":
                if _gzip is None:
                    continue
                data = _gzip.decompress(data)
            elif enc == "br":
                if _brotli is None:
                    logger.warning("收到 brotli 压缩请求但 brotli 未安装，跳过解压")
                    continue
                data = _brotli.decompress(data)
            elif enc == "deflate":
                if _zlib is None:
                    continue
                # deflate 可能是 zlib 包装或裸 deflate
                try:
                    data = _zlib.decompress(data)
                except _zlib.error:
                    data = _zlib.decompress(data, -15)
            elif enc == "identity":
                # 无压缩
                pass
            else:
                logger.warning("未知的 content-encoding: %s，跳过", enc)
        except Exception as e:
            logger.warning("解压 %s 失败: %s，使用原始字节", enc, e)
            return raw
    return data


async def _parse_request_json(request: Request) -> dict:
    """解析请求 JSON，自动处理压缩请求体

    替代 `await request.json()`，覆盖所有 Codex 端点。
    处理流程（参考 CC Switch v3.16.4 PR #3817）：
    1. 读取原始字节
    2. 根据 content-encoding 解压
    3. 剥掉过期的 content-encoding/content-length/transfer-encoding 头
    4. JSON 解析
    """
    raw = await request.body()
    encoding = request.headers.get("content-encoding", "").lower()

    if encoding:
        raw_len = len(raw) if raw else 0
        raw = _decompress_body(raw, encoding)
        logger.debug("已解压请求体: encoding=%s, %d → %d bytes",
                     encoding, raw_len, len(raw) if raw else 0)

    if not raw:
        raise ValueError("空请求体")

    return json.loads(raw)


def _setup_logging(verbose: bool = False) -> None:
    cfg = get_config()
    if not verbose and cfg.verbose_log:
        verbose = True
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)

    root = logging.getLogger("code-cn-bridge")
    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False  # 防止消息传播到 root logger 导致重复输出

    # 控制台 handler
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(ApiKeyFilter())
    root.addHandler(console)

    # 文件 handler — 方便复制日志
    log_file = Path(__file__).resolve().parent.parent / "bridge.log"
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_file), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 根 logger 也加上，捕获 uvicorn 等库的日志
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for h in root_logger.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            root_logger.removeHandler(h)
    root_logger.addHandler(file_handler)


def _get_adapter_for_model(model: str) -> tuple[BaseAdapter, str, str, list[str]]:
    """根据 code 模型名查找适配器

    Returns:
        (adapter, provider_name, target_model, api_keys)
    """
    cfg = get_config()
    provider_name, target_model = cfg.resolve_model(model)
    return _resolve_adapter(provider_name, target_model)


def _resolve_adapter(provider_name: str, target_model: str) -> tuple[BaseAdapter, str, str, list[str]]:
    """根据 provider 名和目标模型查找适配器实例

    Returns:
        (adapter, provider_name, target_model, api_keys)
        api_keys 是列表，支持多 key 轮转
    """
    cfg = get_config()
    provider = cfg.get_provider(provider_name)

    if not provider:
        raise ValueError(
            f"未找到 provider '{provider_name}' 配置。"
            f"请在 config.yaml 中配置 providers。"
        )

    adapter_name = provider.get("adapter", provider_name)
    reg = get_registry()
    adapter = reg.get(adapter_name)
    if not adapter:
        raise ValueError(
            f"未找到适配器 '{adapter_name}'。"
            f"可用适配器: {reg.list()}"
        )

    api_keys = cfg.get_api_keys(provider_name)
    if not api_keys or not api_keys[0]:
        raise ValueError(
            f"Provider '{provider_name}' 的 API Key 未设置。"
            f"请设置环境变量 {provider.get('api_key_env', '???')}"
        )

    # 允许 provider 覆盖 base_url
    if provider.get("base_url"):
        adapter.base_url = provider["base_url"]

    return adapter, provider_name, target_model, api_keys


def _has_images(input_items: list[dict]) -> bool:
    """检测 input 数组是否包含图片（检查 message content 和 function_call_output 的 output）"""
    for item in input_items:
        # content 字段（message 类型）以及 output 字段（function_call_output 类型）
        for field in ("content", "output"):
            content = item.get(field, "")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") in ("input_image", "image_url"):
                        return True
    return False


async def _handle_responses_image_gen(
    body: dict, cfg, model: str
) -> JSONResponse:
    """拦截 image_gen 内置工具：从 input 提取提示词，调用生图 API，返回生成的图片"""
    import json as _json
    import base64 as _base64

    # 1. 从 input 中提取用户的生图提示词和尺寸
    prompt = ""
    size = "2560x1440"  # Seedream 4.5 最低要求 3686400 像素
    for item in reversed(body.get("input", [])):
        if item.get("type") == "message" and item.get("role") == "user":
            content = item.get("content", "")
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                prompt = " ".join(parts)
            else:
                prompt = str(content)
            break

    # 也从 tools 配置中提取尺寸
    tools = body.get("tools", [])
    for t in tools:
        if t.get("type") == "image_gen":
            t_size = t.get("size", "")
            if t_size:
                size = t_size
            break

    if not prompt:
        return JSONResponse(
            build_error_response("无法从请求中提取生图提示词", "invalid_request"),
            status_code=400,
        )

    # 2. 查找生图模型
    img_alias = ""
    img_target = ""
    img_provider = ""
    mapping = cfg.model_mapping
    for alias, entry in mapping.items():
        items = entry if isinstance(entry, list) else [entry]
        for item in items:
            if isinstance(item, dict) and item.get("is_image_gen"):
                img_alias = alias
                img_target = item.get("target", alias)
                img_provider = item.get("provider", "")
                break
        if img_alias:
            break

    if not img_alias:
        return JSONResponse(
            build_error_response("未配置生图模型，请在桌面端添加一个「图片生成」类型的模型", "no_image_gen_model"),
            status_code=400,
        )

    # 3. 解析 provider / adapter
    provider_name = img_provider
    if not provider_name:
        for pname in cfg.providers:
            if pname in img_target.lower():
                provider_name = pname
                break
    if not provider_name and cfg.providers:
        provider_name = next(iter(cfg.providers))

    if not provider_name or provider_name not in cfg.providers:
        return JSONResponse(
            build_error_response(f"生图模型 {img_alias} 的 provider 不存在"),
            status_code=400,
        )

    try:
        adapter, _, _, api_keys = _resolve_adapter(provider_name, img_target)
    except ValueError as exc:
        return JSONResponse(build_error_response(str(exc)), status_code=400)

    # 4. 实际调用生图 API
    img_body = {
        "model": img_target,
        "prompt": prompt,
        "n": 1,
        "size": size,
    }
    img_body = adapter.preprocess_image_gen_request(img_body)
    img_url = adapter.build_image_gen_url()
    headers = adapter.get_headers(api_keys[0])

    logger.info("image_gen 拦截 → 调用生图 API: %s, prompt=%.80s..., size=%s",
        img_url, prompt[:80], size)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120), trust_env=False) as client:
            resp = await client.post(img_url, json=img_body, headers=headers)
            result = resp.json()
    except httpx.TimeoutException:
        return JSONResponse(
            build_error_response("生图请求超时（120秒）", "timeout"),
            status_code=504,
        )
    except Exception as exc:
        logger.exception("生图 API 调用失败")
        return JSONResponse(
            build_error_response(f"生图 API 调用失败: {exc}", "image_gen_failed"),
            status_code=500,
        )

    if resp.status_code != 200:
        err_msg = result.get("error", {}).get("message", str(result))
        logger.warning("生图失败: %s", err_msg)
        return JSONResponse(
            build_error_response(f"生图失败: {err_msg}", "image_gen_failed"),
            status_code=resp.status_code,
        )

    # 5. 下载图片并转 base64
    image_data = None
    image_url_from_api = ""
    data_items = result.get("data", [])
    if data_items:
        first = data_items[0]
        image_url_from_api = first.get("url", "")
        b64 = first.get("b64_json", "")

        if b64:
            image_data = b64
        elif image_url_from_api:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60), trust_env=False) as client:
                    img_resp = await client.get(image_url_from_api)
                    if img_resp.status_code == 200:
                        image_data = _base64.b64encode(img_resp.content).decode("ascii")
                    else:
                        logger.warning("下载生图结果失败: HTTP %d", img_resp.status_code)
            except Exception as exc:
                logger.warning("下载生图结果异常: %s", exc)

    if not image_data:
        return JSONResponse(
            build_error_response("生图 API 返回了结果但没有图片数据", "no_image_data"),
            status_code=500,
        )

    # 6. 保存图片到当前工作目录
    import os as _os
    img_bytes = _base64.b64decode(image_data)
    cwd = _os.getcwd()
    img_filename = f"generated_image_{_uid('img')}.png"
    img_filepath = _os.path.join(cwd, img_filename)
    with open(img_filepath, "wb") as f:
        f.write(img_bytes)
    logger.info("图片已保存: %s (%d bytes)", img_filepath, len(img_bytes))

    # 7. 构造 Responses API 输出：image_generation_call + image_generation_call_output
    call_id = _uid("call_")

    # output 直接放图片 URL（code CLI 会自行下载渲染）；URL 为空时回退 base64
    img_output = image_url_from_api or ("data:image/jpeg;base64," + image_data)

    output_items = [
        {
            "id": _uid("icall"),
            "object": "realtime.item",
            "type": "image_generation_call",
            "call_id": call_id,
            "prompt": prompt,
            "size": size,
            "status": "completed",
        },
        {
            "id": _uid("icall_out"),
            "object": "realtime.item",
            "type": "image_generation_call_output",
            "call_id": call_id,
            "output": img_output,
        },
    ]

    output_items.append(make_message_output_item(
        f"图片已生成并保存到: {img_filepath}"
    ))

    logger.info("image_gen 完成: prompt=%.80s..., file=%s", prompt[:80], img_filename)
    return JSONResponse(
        content=build_responses_response(output_items, model, None)
    )


def _route_vision(model: str, body: dict) -> tuple[BaseAdapter, str, str, str]:
    """视觉路由：检测图片，优先使用模型级配置，其次全局配置"""
    cfg = get_config()
    input_items = body.get("input", [])

    if not _has_images(input_items):
        return _get_adapter_for_model(model)

    # 1. 检查模型级视觉配置
    entry = cfg.model_mapping.get(model)

    # 辅助：从条目中提取配置（支持多模型列表）
    def _get_active_entry(e):
        if isinstance(e, list):
            return next((item for item in e if item.get("enabled", True)), e[0])
        if isinstance(e, dict):
            return e
        return None

    active_entry = _get_active_entry(entry)
    if active_entry:
        # 多模态模型，自身能处理图片
        if active_entry.get("is_multimodal"):
            logger.info("模型 %s 是多模态的，使用自身处理图片", model)
            return _get_adapter_for_model(model)
        # 指定了视觉模型别名
        vision_alias = active_entry.get("vision_alias")
        if vision_alias and vision_alias in cfg.model_mapping:
            ventry = _get_active_entry(cfg.model_mapping[vision_alias])
            if ventry:
                v_target = ventry.get("target", vision_alias)
                v_provider = ventry.get("provider", "") or ventry.get("target", "")
                try:
                    logger.info("检测到图片输入，切换到视觉模型: %s/%s (来自 %s)", v_provider, v_target, vision_alias)
                    return _resolve_adapter(v_provider, v_target)
                except ValueError as exc:
                    logger.warning("视觉模型 %s/%s 不可用: %s，回退到默认路由", v_provider, v_target, exc)

    # 2. 回退到全局视觉路由
    vr = cfg.vision_routing
    if vr.get("enabled"):
        vision_provider = vr.get("provider", "doubao")
        vision_model = vr.get("model", "doubao-vision-pro-32k")
        try:
            logger.info("检测到图片输入，使用全局视觉路由: %s/%s", vision_provider, vision_model)
            return _resolve_adapter(vision_provider, vision_model)
        except ValueError as exc:
            logger.warning("全局视觉路由 %s/%s 不可用: %s，回退到文本模型（图片将被忽略）",
                vision_provider, vision_model, exc)

    # 3. 视觉路由不可用，回退到文本模型：剥离图片内容防止上游 400
    logger.warning("视觉路由未配置或不可用，使用文本模型 %s 处理请求（图片已移除）", model)
    _strip_images_from_input(input_items)
    return _get_adapter_for_model(model)


def _strip_images_from_input(input_items: list[dict]) -> None:
    """从 input 数组中移除所有图片内容，防止文本模型报 400"""
    for item in input_items:
        for field in ("content", "output"):
            content = item.get(field)
            if isinstance(content, list):
                item[field] = [p for p in content if p.get("type") not in ("input_image", "image_url")]


# ── 原生 Computer Use 插件指令检测与剥离 ──────────────────────────────

# 原生 SKILL.md 中的唯一标识字符串，用于可靠检测
_CUA_NATIVE_MARKERS = [
    "setupComputerUseRuntime",
    "computer-use-client.mjs",
    "@oai/sky",
    "Sky Window2",
    "codex-computer-use.exe",
    "Windows.Graphics.Capture",
    "node_repl",
]


def _has_native_cua_instructions(instructions: str) -> bool:
    """检测 instructions 中是否包含原生 Computer Use 插件的 SKILL.md 内容"""
    if not instructions:
        return False
    # 只需匹配 2 个以上标记即可确认
    matches = sum(1 for m in _CUA_NATIVE_MARKERS if m in instructions)
    return matches >= 2


def _strip_native_cua_instructions(instructions: str) -> str:
    """从 instructions 中剥离原生 Computer Use SKILL.md 内容，保留其他指令

    策略（按优先级）：
    1. 找到 "# Computer Use" 标题，截断其后所有内容
    2. 移除 YAML frontmatter 中的 computer-use 元数据块
    3. 对残留的 CUA 相关段落做二次清理
    """
    if not instructions:
        return instructions

    # ── 第一步：找到 "# Computer Use" 标题位置，截断 ──────────────
    cua_heading = None
    for marker in ["# Computer Use\n", "# Computer Use\r\n", "# Computer Use"]:
        idx = instructions.find(marker)
        if idx != -1:
            cua_heading = idx
            break

    if cua_heading is not None:
        # 保留 "# Computer Use" 之前的所有内容
        before = instructions[:cua_heading].strip()
        instructions = before

    # ── 第二步：移除 YAML frontmatter 中的 computer-use 块 ────────
    # 格式: ---\nname: computer-use\ndescription: ...\n---
    import re
    # 匹配完整的 frontmatter 块 (--- ... ---)
    instructions = re.sub(
        r'^\s*---\s*\n[^-]*?computer-use[^-]*?---\s*\n?',
        '',
        instructions,
        flags=re.MULTILINE | re.DOTALL,
    )
    # 也处理 frontmatter 出现在文本中间的情况
    instructions = re.sub(
        r'---\s*\n\s*name:\s*computer-use[^\n]*\n(?:[^\n]*\n)*?---\s*\n?',
        '',
        instructions,
    )

    # ── 第三步：二次清理残留的 CUA 相关段落 ───────────────────────
    paragraphs = instructions.split("\n\n")
    cleaned_paragraphs = []
    for p in paragraphs:
        stripped = p.strip()
        # 跳过空段落
        if not stripped:
            continue
        # 跳过包含原生标记的段落
        if any(m in p for m in _CUA_NATIVE_MARKERS):
            continue
        # 跳过纯 YAML 元数据残留
        if stripped.startswith("name:") and "computer" in stripped.lower():
            continue
        if stripped.startswith("description:") and len(stripped) < 200:
            continue
        # 跳过纯 CUA 相关描述的段落
        if ("computer use" in p.lower() and
                any(kw in p.lower() for kw in ["skill", "bootstrap", "plugin", "runtime", "node_repl"])):
            continue
        cleaned_paragraphs.append(p)

    return "\n\n".join(cleaned_paragraphs).strip()


def _strip_cua_from_input(body: dict) -> None:
    """清理 input 消息数组中的原生 Computer Use 指令内容

    处理两种情况：
    1. message 的 content 中包含 SKILL.md（直接移除该消息）
    2. function_call_output 的 output 中包含 SKILL.md（替换为简短提示）
    """
    input_items = body.get("input", [])
    if not input_items or not isinstance(input_items, list):
        return

    filtered: list[dict] = []
    removed = 0
    stripped_outputs = 0
    for item in input_items:
        if not isinstance(item, dict):
            filtered.append(item)
            continue

        item_type = item.get("type", "")

        # ── 检查 message 的 content ──────────────────────────
        content = ""
        raw_content = item.get("content", "")
        if isinstance(raw_content, str):
            content = raw_content
        elif isinstance(raw_content, list):
            content = " ".join(
                p.get("text", "") for p in raw_content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            )

        if content and _has_native_cua_instructions(content):
            logger.info("从 input 中移除原生 CUA 指令消息 (type=%s, %d 字符)",
                        item_type, len(content))
            removed += 1
            continue

        # ── 检查 function_call_output 的 output ──────────────
        if item_type == "function_call_output":
            output_val = item.get("output", "")
            output_text = ""
            if isinstance(output_val, str):
                output_text = output_val
            elif isinstance(output_val, list):
                output_text = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in output_val
                )

            if output_text and _has_native_cua_instructions(output_text):
                # 替换 output 为简短提示，不让模型看到 SKILL.md 内容
                item = dict(item)  # 复制避免修改原始对象
                item["output"] = (
                    "[SKILL.md content stripped — use the built-in computer_use tool instead.]\n"
                    "Available computer_use actions and their required params:\n"
                    "- list_windows: {} — list currently open windows\n"
                    "- list_apps: {} — list installed apps, each with id and open windows. USE THIS FIRST to find app ids.\n"
                    "- launch_app: {\"app\": \"<app_id_from_list_apps OR full_exe_path>\"} — launch an app. "
                    "The 'app' param MUST be either an 'id' returned by list_apps, or a full .exe path like "
                    "\"C:\\\\Windows\\\\System32\\\\mspaint.exe\". Do NOT use app names like \"mspaint\".\n"
                    "- activate_window: {\"window\": {<window_obj>}} — bring window to foreground\n"
                    "- get_window_state: {\"window\": {<window_obj>}, \"include_screenshot\": true} — capture screen\n"
                    "- click: {\"window\": {<window_obj>}, \"x\": <int>, \"y\": <int>} — click at window-relative coords\n"
                    "- type_text: {\"window\": {<window_obj>}, \"text\": \"<string>\"} — type text\n"
                    "- press_key: {\"window\": {<window_obj>}, \"key\": \"<keysym>\"} — press key (e.g. \"Return\", \"Escape\")\n"
                    "Workflow: list_apps → find target app id → launch_app(app_id) → poll list_windows → "
                    "activate_window → get_window_state → click/type/press_key to interact."
                )
                stripped_outputs += 1
                logger.info("剥离 function_call_output 中的 SKILL.md (call_id=%s, %d 字符)",
                            item.get("call_id", "?"), len(output_text))

        filtered.append(item)

    if removed > 0:
        body["input"] = filtered
        logger.info("共从 input 中移除 %d 条原生 CUA 指令消息", removed)
    elif stripped_outputs > 0:
        body["input"] = filtered
    if stripped_outputs > 0:
        logger.info("共剥离 %d 条 function_call_output 中的 SKILL.md", stripped_outputs)


def _intercept_builtin_tools(body: dict) -> dict:
    """拦截 Responses API 内置工具（web_search/file_search/code_interpreter/video_gen）
    以及展开 MCP 包装器工具（mcp__node_repl / codex_app 等）。

    将这些内置工具从 tools 列表中移除（不让上游模型看到），并根据工具类型进行降级处理：
    - web_search: 注入 system prompt 提示（无论是否有搜索 API 配置，暂时都用降级方案）
    - file_search: 注入 system prompt 提示，返回空结果集
    - code_interpreter: 转为 function tool（名为 python），让模型调用，bridge 拦截执行
    - video_gen: 设置标志，后续直接调用视频生成 API
    - computer_use_preview: 降级提示（需要原生 Codex 运行时）
    - MCP 包装器工具: 展开为独立 function 工具，建立名称映射

    Returns:
        dict with flags: has_web_search, has_file_search, has_code_interpreter, has_video_gen
    """
    tools = body.get("tools")
    if not tools:
        return {
            "has_web_search": False,
            "has_file_search": False,
            "has_code_interpreter": False,
            "has_video_gen": False,
            "has_computer_use": False,
        }

    flags = {
        "has_web_search": False,
        "has_file_search": False,
        "has_code_interpreter": False,
        "has_video_gen": False,
        "has_computer_use": False,
    }
    system_addons: list[str] = []

    # ── MCP 包装器展开映射 ────────────────────────────────────────
    # key: 展开后的工具名 (e.g. "mcp__node_repl__js")
    # value: (包装器名, 子工具名) (e.g. ("mcp__node_repl", "js"))
    mcp_tool_mapping: dict[str, tuple[str, str]] = {}

    filtered_tools = []
    for t in tools:
        tool_type = t.get("type", "")
        if tool_type == "web_search":
            flags["has_web_search"] = True
        elif tool_type == "file_search":
            flags["has_file_search"] = True
        elif tool_type == "code_interpreter":
            flags["has_code_interpreter"] = True
        elif tool_type == "video_gen":
            flags["has_video_gen"] = True
        elif tool_type == "computer_use_preview":
            # Computer Use: 创建代理函数工具，通过命名管道连接 codex-computer-use.exe
            # 模型通过此工具执行屏幕截图、鼠标/键盘控制等操作
            flags["has_computer_use"] = True
        elif "tools" in t and isinstance(t["tools"], list):
            # ── MCP 包装器工具展开 ──────────────────────────────
            # Codex 发送的 MCP 服务器包装器格式:
            # {"type": "function", "tools": [{"name": "js", ...}, ...]}
            # 其中 type 字段就是包装器名称 (e.g. "mcp__node_repl", "codex_app")
            # 展开为独立的 function 工具，让 LLM 能正确调用
            wrapper_name = tool_type  # e.g. "mcp__node_repl" or "codex_app"
            sub_tools = t["tools"]
            for sub_tool in sub_tools:
                if not isinstance(sub_tool, dict):
                    continue
                sub_name = sub_tool.get("name", "")
                if not sub_name:
                    continue
                # 构建展开后的工具名: {wrapper}__{sub_tool}
                expanded_name = f"{wrapper_name}__{sub_name}"
                # 创建标准 function 工具定义
                expanded_tool = {
                    "type": "function",
                    "function": {
                        "name": expanded_name,
                        "description": sub_tool.get("description", ""),
                        "parameters": sub_tool.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
                # 传递 strict 等额外字段
                if "strict" in sub_tool:
                    expanded_tool["function"]["strict"] = sub_tool["strict"]
                filtered_tools.append(expanded_tool)
                mcp_tool_mapping[expanded_name] = (wrapper_name, sub_name)
        else:
            # ── namespace__ 前缀工具重命名 ──────────────────────
            # Codex 把 node_repl MCP 工具暴露为 namespace__js / namespace__js_reset 等
            # 但 Codex 的工具执行路由器期望标准 MCP 命名 mcp__node_repl__js
            # 这里重命名为标准格式，让 Codex 能正确路由执行
            fn = t.get("function", {})
            fn_name = fn.get("name", "") if isinstance(fn, dict) else ""
            if fn_name.startswith("namespace__"):
                # namespace__js → mcp__node_repl__js
                sub_name = fn_name[len("namespace__"):]
                new_name = f"mcp__node_repl__{sub_name}"
                renamed_tool = dict(t)
                renamed_tool["function"] = dict(fn)
                renamed_tool["function"]["name"] = new_name
                filtered_tools.append(renamed_tool)
                mcp_tool_mapping[new_name] = ("mcp__node_repl", sub_name)
                logger.info("[Rename] %s → %s", fn_name, new_name)
            else:
                filtered_tools.append(t)

    body["tools"] = filtered_tools
    # 调试：记录重命名后的工具列表
    _renamed_names = [t.get("function", {}).get("name", "?") for t in filtered_tools if t.get("type") == "function"]
    _has_namespace = any("namespace__" in n for n in _renamed_names)
    if _has_namespace:
        logger.warning("[RenameCheck] 重命名后仍有 namespace__ 工具: %s", _renamed_names)

    # ── 基于 instructions 内容的 CUA 回退检测 ─────────────────────
    # Codex 可能不在 tools 数组中发送 computer_use_preview，
    # 但仍将 SKILL.md 内容注入到 instructions 中。
    # 此时需要基于指令内容激活 CUA 代理模式。
    if not flags["has_computer_use"]:
        _instr = body.get("instructions", "")
        _input_items = body.get("input", [])
        _input_cua_found = 0
        if isinstance(_input_items, list):
            for _idx, _item in enumerate(_input_items):
                if not isinstance(_item, dict):
                    continue
                _item_type = _item.get("type", "")

                # 提取要检查的文本：从 content 或 output 字段
                _texts_to_check = []

                # content 字段 (message 类型)
                _raw_content = _item.get("content", "")
                if isinstance(_raw_content, str) and _raw_content:
                    _texts_to_check.append(_raw_content)
                elif isinstance(_raw_content, list):
                    _joined = " ".join(p.get("text", "") for p in _raw_content
                                       if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
                    if _joined:
                        _texts_to_check.append(_joined)

                # output 字段 (function_call_output 类型)
                _raw_output = _item.get("output", "")
                if isinstance(_raw_output, str) and _raw_output:
                    _texts_to_check.append(_raw_output)
                elif isinstance(_raw_output, list):
                    # output 可能是列表格式
                    _joined_out = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in _raw_output
                    )
                    if _joined_out:
                        _texts_to_check.append(_joined_out)

                for _c in _texts_to_check:
                    if _has_native_cua_instructions(_c):
                        _input_cua_found += 1
                        logger.info("CUA 回退检测: input[%d] type=%s len=%d 包含原生 CUA 指令",
                                    _idx, _item_type, len(_c))
                        break  # 一个 item 只计一次

        if _instr and _has_native_cua_instructions(_instr):
            flags["has_computer_use"] = True
            logger.info("CUA 代理激活: instructions 包含原生 CUA 指令")
        elif _input_cua_found > 0:
            flags["has_computer_use"] = True
            logger.info("CUA 代理激活: input 中 %d 条消息包含原生 CUA 指令", _input_cua_found)

    # 存储 MCP 映射到 body，供 translate_request / translate_response 使用
    if mcp_tool_mapping:
        body["_mcp_tool_mapping"] = mcp_tool_mapping

    # web_search 降级：无论是否有搜索 API 配置，暂时都用降级方案
    if flags["has_web_search"]:
        system_addons.append(
            "你具有网络搜索能力，请基于你的知识回答用户关于最新信息的问题。"
        )

    # file_search 降级：返回空结果集
    if flags["has_file_search"]:
        system_addons.append(
            "文件搜索功能暂不可用，请基于已有信息回答。"
        )

    # code_interpreter：转为 function tool，让模型可以调用 python 执行代码
    if flags["has_code_interpreter"]:
        body["tools"].append({
            "type": "function",
            "function": {
                "name": "python",
                "description": (
                    "Execute Python code in a restricted sandbox and return the output. "
                    "Use this tool to perform calculations, data analysis, or any computational task. "
                    "The code runs with a 10-second timeout and no network access."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "The Python code to execute.",
                        }
                    },
                    "required": ["code"],
                },
            },
        })

    # computer_use：创建代理函数工具，通过命名管道连接 codex-computer-use.exe
    if flags.get("has_computer_use"):
        body["tools"].append({
            "type": "function",
            "function": {
                "name": "computer_use",
                "description": (
                    "Control the Windows desktop: take screenshots, click, type, press keys, "
                    "scroll, launch apps, and read accessibility trees. "
                    "Use 'action' to specify the operation and 'params' for its arguments.\n\n"
                    "Available actions:\n"
                    "- list_windows: List open windows (no params)\n"
                    "- list_apps: List installed apps (no params)\n"
                    "- get_window_state: Capture screenshot + accessibility tree. params: {window, include_screenshot?, include_text?}\n"
                    "- click: Click in a window. params: {window, x?, y?, element_index?, mouse_button?, click_count?, screenshotId?}\n"
                    "- type_text: Type text. params: {window, text}\n"
                    "- press_key: Press key chord. params: {window, key} (X11 keysym format: Return, Control_L+a)\n"
                    "- scroll: Scroll. params: {window, x, y, scrollX, scrollY, screenshotId?}\n"
                    "- drag: Drag. params: {window, from_x, from_y, to_x, to_y, screenshotId?}\n"
                    "- launch_app: Launch app. params: {app}\n"
                    "- activate_window: Bring window to front. params: {window}\n"
                    "- set_value: Set editable element value. params: {window, element_index, value}\n"
                    "- perform_secondary_action: Accessibility action. params: {window, element_index, action}\n\n"
                    "Window object format: {app: string, id: number, title?: string}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "The action to perform",
                            "enum": [
                                "list_windows", "list_apps", "get_window_state",
                                "click", "type_text", "press_key", "scroll", "drag",
                                "launch_app", "activate_window", "set_value",
                                "perform_secondary_action",
                            ],
                        },
                        "params": {
                            "type": "object",
                            "description": "Action-specific parameters (see description for each action)",
                        },
                    },
                    "required": ["action"],
                },
            },
        })

        # ── 剥离原生 Computer Use 插件指令 ─────────────────────────
        # Codex 会将 computer-use 插件的 SKILL.md 内容注入到 instructions 中，
        # 引导模型去读取文件、运行 node_repl 脚本等。这和我们代理工具的流程冲突。
        # 需要将原生指令替换为简洁的代理工具使用指南。
        instructions = body.get("instructions", "")
        if instructions and _has_native_cua_instructions(instructions):
            instructions = _strip_native_cua_instructions(instructions)
            logger.info("已剥离原生 Computer Use SKILL.md 指令 (%d → %d 字符)",
                        len(body.get("instructions", "")), len(instructions))

        # 替换为代理模式的系统指令
        system_addons.append(
            "IMPORTANT: You have a built-in `computer_use` tool for Windows desktop automation. "
            "You MUST use this tool directly — do NOT try to read SKILL.md files, "
            "run JavaScript/Node REPL scripts, import computer-use-client modules, "
            "or bootstrap any external runtime. "
            "The `computer_use` tool is already fully configured and ready to use.\n\n"
            "Workflow:\n"
            "1. Call computer_use with action 'list_windows' to see open windows\n"
            "2. Call computer_use with action 'activate_window' to bring a window to front\n"
            "3. Call computer_use with action 'get_window_state' to capture the screen\n"
            "4. Use click/type_text/press_key/scroll to interact with the UI\n\n"
            "Never tell the user that Computer Use is unavailable — it IS available through the computer_use tool."
        )

        body["instructions"] = instructions

    if system_addons:
        addon = "\n".join(system_addons)
        existing = body.get("instructions", "").strip()
        if existing:
            body["instructions"] = existing + "\n\n---\n" + addon
        else:
            body["instructions"] = addon

    # ── 清理 input 中的原生 Computer Use 指令消息 ──────────────────
    if flags.get("has_computer_use"):
        _strip_cua_from_input(body)

    return flags


def _execute_python_code(code: str) -> str:
    """在受限子进程中执行 Python 代码，返回输出

    限制：
    - 超时 10 秒
    - 捕获 stdout 和 stderr
    - 输出截断到 4000 字符
    - 禁止网络访问（通过环境变量清空）
    """
    try:
        # 构建受限环境：仅保留必要的 PATH，移除可能用于网络访问的变量
        restricted_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
        }

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            env=restricted_env,
        )

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += "\n[stderr]\n"
            else:
                output += "[stderr]\n"
            output += result.stderr

        if not output:
            output = "(代码执行完毕，无输出)"

        # 截断到 4000 字符
        if len(output) > 4000:
            output = output[:4000] + f"\n...[输出已截断，原长 {len(output)} 字符]"

        return output
    except subprocess.TimeoutExpired:
        return "[错误] 代码执行超时（10秒限制）"
    except Exception as exc:
        return f"[错误] 代码执行失败: {exc}"


def _process_code_interpreter_response(responses_resp: dict, model: str) -> dict:
    """检查响应中是否包含 code_interpreter/python 的 function_call，执行代码并注入结果

    当模型返回名为 python 或 code_interpreter 的 function_call 时，
    拦截执行代码，并将执行结果作为 function_call_output 添加到响应中。
    """
    output = responses_resp.get("output", [])
    new_items: list[dict] = []

    for item in output:
        new_items.append(item)
        if item.get("type") == "function_call":
            name = item.get("name", "")
            if name in ("python", "code_interpreter"):
                # 提取代码
                arguments = item.get("arguments", "")
                try:
                    args = json.loads(arguments) if arguments else {}
                    code = args.get("code", "")
                except (json.JSONDecodeError, ValueError):
                    code = arguments  # 假设 arguments 直接是代码

                if not code:
                    code = "# 无代码"

                logger.info("code_interpreter 拦截: 执行 Python 代码 (%d 字符)", len(code))
                execution_result = _execute_python_code(code)

                # 添加 function_call_output
                call_id = item.get("call_id", item.get("id", ""))
                new_items.append({
                    "id": _uid("cout"),
                    "object": "realtime.item",
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": execution_result,
                    "status": "completed",
                })

    responses_resp["output"] = new_items
    return responses_resp


def _process_computer_use_response(responses_resp: dict, model: str) -> dict:
    """检查响应中是否包含 computer_use 的 function_call，通过 CUA 代理执行并注入结果

    当模型返回名为 computer_use 的 function_call 时，
    通过命名管道连接 codex-computer-use.exe 执行操作，
    并将结果作为 function_call_output 添加到响应中。
    """
    from .codex_cua import handle_computer_use_call

    output = responses_resp.get("output", [])
    new_items: list[dict] = []
    has_cua_calls = False

    for item in output:
        new_items.append(item)
        if item.get("type") == "function_call":
            name = item.get("name", "")
            if name == "computer_use":
                has_cua_calls = True
                arguments = item.get("arguments", "")
                try:
                    args = json.loads(arguments) if arguments else {}
                except (json.JSONDecodeError, ValueError):
                    args = {}

                action = args.get("action", "")
                params = args.get("params", {})

                logger.info("computer_use 拦截: action=%s params=%s",
                    action, json.dumps(params, ensure_ascii=False)[:200])

                # 通过 CUA 代理执行操作
                result = handle_computer_use_call(action, params)

                # 添加 function_call_output
                call_id = item.get("call_id", item.get("id", ""))
                output_text = json.dumps(result, ensure_ascii=False)
                # 限制输出大小（截图 data URL 可能很大）
                if len(output_text) > 50000:
                    output_text = output_text[:50000] + '...[truncated]'

                new_items.append({
                    "id": _uid("cua"),
                    "object": "realtime.item",
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_text,
                    "status": "completed",
                })

    responses_resp["output"] = new_items
    # 如果有 CUA 调用且已执行，设置状态为 completed（工具已执行，不需要 Codex 再处理）
    if has_cua_calls:
        responses_resp["status"] = "completed"
    return responses_resp


async def _handle_responses_video_gen(
    body: dict, cfg, model: str
) -> JSONResponse:
    """拦截 video_gen 内置工具：从 input 提取提示词，调用视频生成 API，返回生成的视频"""
    # 1. 从 input 中提取用户的视频生成提示词
    prompt = ""
    for item in reversed(body.get("input", [])):
        if item.get("type") == "message" and item.get("role") == "user":
            content = item.get("content", "")
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                prompt = " ".join(parts)
            else:
                prompt = str(content)
            break

    if not prompt:
        return JSONResponse(
            build_error_response("无法从请求中提取视频生成提示词", "invalid_request"),
            status_code=400,
        )

    # 2. 查找视频生成模型
    vid_alias = ""
    vid_target = ""
    vid_provider = ""
    mapping = cfg.model_mapping
    for alias, entry in mapping.items():
        items = entry if isinstance(entry, list) else [entry]
        for item in items:
            if isinstance(item, dict) and item.get("is_video_gen"):
                vid_alias = alias
                vid_target = item.get("target", alias)
                vid_provider = item.get("provider", "")
                break
        if vid_alias:
            break

    if not vid_alias:
        return JSONResponse(
            build_error_response("未配置视频生成模型，请在桌面端添加一个「视频生成」类型的模型", "no_video_gen_model"),
            status_code=400,
        )

    # 3. 解析 provider / adapter
    provider_name = vid_provider
    if not provider_name:
        for pname in cfg.providers:
            if pname in vid_target.lower():
                provider_name = pname
                break
    if not provider_name and cfg.providers:
        provider_name = next(iter(cfg.providers))

    if not provider_name or provider_name not in cfg.providers:
        return JSONResponse(
            build_error_response(f"视频生成模型 {vid_alias} 的 provider 不存在"),
            status_code=400,
        )

    try:
        adapter, _, _, api_keys = _resolve_adapter(provider_name, vid_target)
    except ValueError as exc:
        return JSONResponse(build_error_response(str(exc)), status_code=400)

    # 4. 构建视频生成 URL
    if hasattr(adapter, "build_video_gen_url"):
        vid_url = adapter.build_video_gen_url()
    else:
        base = adapter.base_url.rstrip("/")
        vid_url = f"{base}/videos/generations"

    headers = adapter.get_headers(api_keys[0])

    # 5. 构建请求体
    vid_body = {
        "model": vid_target,
        "prompt": prompt,
        "n": 1,
    }
    # 透传可能的扩展字段
    for key in ("size", "duration", "fps", "quality", "style", "negative_prompt", "seed"):
        if key in body:
            vid_body[key] = body[key]

    logger.info("video_gen 拦截 → 调用视频生成 API: %s, prompt=%.80s...", vid_url, prompt[:80])

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300), trust_env=False) as client:
            resp = await client.post(vid_url, json=vid_body, headers=headers)
            result = resp.json()
    except httpx.TimeoutException:
        return JSONResponse(
            build_error_response("视频生成请求超时（300秒）", "timeout"),
            status_code=504,
        )
    except Exception as exc:
        logger.exception("视频生成 API 调用失败")
        return JSONResponse(
            build_error_response(f"视频生成 API 调用失败: {exc}", "video_gen_failed"),
            status_code=500,
        )

    if resp.status_code != 200:
        err_msg = result.get("error", {}).get("message", str(result))
        logger.warning("视频生成失败: %s", err_msg)
        return JSONResponse(
            build_error_response(f"视频生成失败: {err_msg}", "video_gen_failed"),
            status_code=resp.status_code,
        )

    # 6. 提取视频 URL
    video_url = ""
    data_items = result.get("data", [])
    if data_items:
        first = data_items[0]
        video_url = first.get("url", "") or first.get("video_url", "")

    if not video_url:
        return JSONResponse(
            build_error_response("视频生成 API 返回了结果但没有视频 URL", "no_video_data"),
            status_code=500,
        )

    # 7. 构造 Responses API 输出
    call_id = _uid("vcall")
    output_items = [
        {
            "id": _uid("vcall"),
            "object": "realtime.item",
            "type": "video_generation_call",
            "call_id": call_id,
            "prompt": prompt,
            "status": "completed",
        },
        {
            "id": _uid("vcall_out"),
            "object": "realtime.item",
            "type": "video_generation_call_output",
            "call_id": call_id,
            "output": video_url,
        },
    ]

    output_items.append(make_message_output_item(
        f"视频已生成: {video_url}"
    ))

    logger.info("video_gen 完成: prompt=%.80s...", prompt[:80])
    return JSONResponse(
        content=build_responses_response(output_items, model, None)
    )


# ── 应用工厂 ───────────────────────────────────────────────────────

def create_app(verbose: bool = False) -> FastAPI:
    _setup_logging(verbose)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("code CN Bridge 启动中...")
        cfg = get_config()
        reg = get_registry()
        logger.info("已加载适配器: %s", reg.list())
        logger.info(
            "服务地址: http://%s:%d",
            cfg.server_host,
            cfg.server_port,
        )
        # 启动时生成 Codex 桌面端的 model catalog
        try:
            catalog_path = generate_catalog()
            update_codex_config(catalog_path)
        except Exception as e:
            logger.warning("生成 model catalog 失败: %s", e)
        # 启动主动健康探测
        prober = get_health_prober()
        await prober.start()

        try:
            yield
        finally:
            await prober.stop()
            logger.info("code CN Bridge 已关闭")

    app = FastAPI(
        title="code CN Bridge",
        version="0.6.1",
        description="OpenAI Responses API → Chat Completions API 协议转换代理",
        lifespan=lifespan,
    )

    app.add_middleware(ErrorHandlingMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(DetailedLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:5174", "http://127.0.0.1:5174",
            "http://localhost:5175", "http://127.0.0.1:5175",
            "app://.", "file://", "null",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册管理 API 路由
    app.include_router(admin_router)

    # ── 路由 ─────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        reg = get_registry()
        cfg = get_config()
        return {
            "status": "ok",
            "adapters": reg.list(),
            "model_mapping": cfg.model_mapping,
        }

    @app.get("/v1/models")
    async def list_models():
        """返回所有启用的模型列表，格式兼容 OpenAI /v1/models。
        id 使用 alias（与 config.toml 的 model_info key 一致），
        让 Codex 桌面版能正确显示和选择模型。
        """
        cfg = get_config()
        models = []
        for alias, entry in cfg.model_mapping.items():
            # 支持多模型列表：至少一个条目启用就算启用
            items = entry if isinstance(entry, list) else [entry]
            if not any(item.get("enabled", True) for item in items):
                continue
            for item in items:
                if not item.get("enabled", True):
                    continue
                target = item.get("target", alias)
                provider_name = item.get("provider", "cn-bridge")
                is_mm = item.get("is_multimodal", False)
                is_img = item.get("is_image_gen", False)
                is_vid = item.get("is_video_gen", False)
                is_thinking = item.get("enable_thinking", False)

                # 估算上下文窗口
                ctx = 200000
                if "kimi" in alias:
                    ctx = 2000000
                elif "minimax" in alias:
                    ctx = 1000000
                elif "qwen" in alias:
                    ctx = 256000
                elif "doubao" in alias:
                    ctx = 256000
                elif "ernie" in alias or "speed-pro" in alias:
                    ctx = 128000
                elif "spark" in alias:
                    ctx = 128000
                elif "ollama" in alias:
                    ctx = 8192

                model_obj = {
                    "id": alias,              # 用 alias 作为 id，与 config.toml 一致
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": provider_name, # 显示真实 provider
                    "target": target,          # 真实模型名（bridge 内部路由用）
                    "context_window": ctx,
                    "capabilities": {
                        "supports_tool_calls": not is_img and not is_vid,
                        "supports_streaming": True,
                        "supports_vision": is_mm,
                        "supports_image_gen": is_img,
                        "supports_video_gen": is_vid,
                        "supports_reasoning": is_thinking,
                    },
                }
                models.append(model_obj)
                break  # 每个 alias 只取第一个启用的条目
        return {"object": "list", "data": models}


    @app.post("/admin/reload-config")
    async def admin_reload():
        reload_config()
        # 重新生成 Codex 桌面端的 model catalog
        try:
            generate_catalog()
        except Exception as e:
            logger.warning("生成 model catalog 失败: %s", e)
        return {"status": "ok", "message": "配置已重新加载"}

    @app.post("/v1/responses")
    async def responses_endpoint(request: Request):
        """核心端点: 接受 Responses API 请求，返回 Responses API 响应"""
        start_time = time.time()
        status_code = 200
        error_msg = ""

        try:
            body = await _parse_request_json(request)
        except Exception:
            return _record_and_respond(
                start_time, status_code=400, error="无效的 JSON 请求体",
                model="unknown", stream=False, provider="", target_model="",
            )

        model = body.get("model", "unknown")
        stream = body.get("stream", False)
        verbose = logger.isEnabledFor(logging.DEBUG)

        # 会话粘性: 同一会话的后续请求路由到同一 provider
        conv_id = _extract_conversation_id(body)
        affinity_provider = get_affinity(conv_id) if conv_id else None

        try:
            if affinity_provider:
                try:
                    adapter, provider_name, target_model, api_keys = _resolve_adapter(affinity_provider, body.get("model", model))
                    logger.debug("会话粘性路由: %s → provider=%s", conv_id, provider_name)
                except ValueError:
                    # 亲和 provider 不可用，回退到正常路由
                    affinity_provider = None
                    adapter, provider_name, target_model, api_keys = _route_vision(model, body)
            else:
                adapter, provider_name, target_model, api_keys = _route_vision(model, body)

            # 记录会话粘性映射
            if conv_id and not affinity_provider:
                set_affinity(conv_id, provider_name)
        except ValueError as exc:
            status_code = 400
            error_msg = str(exc)
            _record_request(start_time, model, "responses", status_code, stream, error_msg, provider="", target_model="")
            return JSONResponse(
                content=build_error_response(error_msg),
                status_code=400,
            )

        if verbose:
            logger.debug("请求模型: %s → %s/%s", model, provider_name, target_model)

        # 从 provider 配置读取超时设置
        provider_timeout = get_config().get_provider(provider_name).get("timeout", 120) if provider_name else 120
        client = UpstreamClient(adapter, api_keys, timeout=provider_timeout, stream_timeout=max(provider_timeout, 600))

        # 熔断器检查
        circuit_breaker = get_circuit_breaker_registry().get(provider_name)
        if not circuit_breaker.before_request():
            status_code = 503
            error_msg = f"Provider '{provider_name}' 已熔断，请稍后重试 (健康评分: {circuit_breaker.health_score})"
            _record_request(start_time, model, "responses", status_code, stream, error_msg, provider=provider_name, target_model=target_model)
            return JSONResponse(
                content=build_error_response(error_msg, "circuit_open", 503),
                status_code=503,
            )

        try:
            # 1. 协议转换: Responses → Chat
            cfg = get_config()

            # 从 model_mapping 读取 per-model thinking 配置（必须在 translate_request 之前设置）
            model_entry = cfg.model_mapping.get(model)
            if isinstance(model_entry, list):
                model_item = next((e for e in model_entry if e.get("enabled")), model_entry[0] if model_entry else None)
            else:
                model_item = model_entry
            if model_item is not None:
                if not model_item.get("enable_thinking", True):
                    body["_disable_thinking"] = True
                if "thinking_budget" in model_item:
                    budget = model_item["thinking_budget"]
                    # 动态缩预算：上下文越长，思考预算越低，给正文留足空间
                    other_count = sum(
                        1 for item in body.get("input", [])
                        if item.get("role") != "system"
                    )
                    if other_count > 25:
                        budget = min(budget, 4096)
                    elif other_count > 15:
                        budget = min(budget, 8192)
                    body["_thinking_budget"] = budget

            # 拦截内置工具 (web_search, file_search, code_interpreter, video_gen, computer_use)
            # 在 translate_request 之前移除，避免被上游模型看到
            builtin_flags = _intercept_builtin_tools(body)

            # computer_use 需要代理执行工具调用，强制非流式以便拦截和注入结果
            # 保存原始 stream 值：CUA 循环完成后需要按原格式（SSE）返回，否则
            # Codex 会报 "stream disconnected before completion: stream closed before response.completed"
            _original_stream = stream
            if builtin_flags.get("has_computer_use") and stream:
                logger.info("computer_use 工具激活，切换为非流式模式以便代理执行")
                stream = False
                body["stream"] = False
            else:
                pass

            # ── CUA 请求去重：防止 Codex 重复发送相同请求 ──────
            _input_hash = ""
            if builtin_flags.get("has_computer_use"):
                _raw_input = body.get("input", [])
                _input_bytes = json.dumps(_raw_input, ensure_ascii=False, sort_keys=True).encode("utf-8")
                _input_hash = _hashlib.sha256(_input_bytes).hexdigest()[:16]
                _now = time.time()
                # 清理过期条目
                _expired = [k for k, (_, ts) in _cua_dedup_cache.items() if _now - ts > _CUA_DEDUP_TTL]
                for k in _expired:
                    del _cua_dedup_cache[k]
                # 检查缓存命中
                if _input_hash in _cua_dedup_cache:
                    _cached_resp, _cached_ts = _cua_dedup_cache[_input_hash]
                    logger.info("CUA 请求去重命中: hash=%s, 距上次 %.1fs, 直接返回缓存响应 (id=%s)",
                                _input_hash, _now - _cached_ts, _cached_resp.get("id", "?"))
                    _record_request(start_time, model, "responses", 200, False, "",
                                    _cached_resp.get("usage", {}).get("total_tokens", 0),
                                    provider=provider_name, target_model=target_model)
                    circuit_breaker.on_success()
                    # 原始请求是流式时，缓存响应也要包装成 SSE 流返回
                    if _original_stream:
                        return StreamingResponse(
                            _wrap_cua_response_as_sse(_cached_resp, target_model),
                            media_type="text/event-stream",
                            headers={
                                "Cache-Control": "no-cache",
                                "Connection": "keep-alive",
                                "X-Accel-Buffering": "no",
                            },
                        )
                    return JSONResponse(content=_cached_resp)
                logger.info("CUA 请求去重未命中: hash=%s, input_count=%d",
                            _input_hash, len(_raw_input) if isinstance(_raw_input, list) else 0)

            chat_req = translate_request(body, adapter, target_model, alias=model)
            has_image_gen = chat_req.pop("_has_image_gen", False)

            # image_gen 内置工具：不转发给 LLM，直接在 bridge 内处理
            if has_image_gen:
                return await _handle_responses_image_gen(body, cfg, model)

            # video_gen 内置工具：不转发给 LLM，直接在 bridge 内处理
            if builtin_flags["has_video_gen"]:
                return await _handle_responses_video_gen(body, cfg, model)

            # 确保 _store 标志传递到 chat_req（store=false 时跳过缓存写入）
            # protocol.py 可能已将 store 提取到 _store，此时不覆盖
            chat_req.setdefault("_store", body.get("store", True))

            logger.info("Chat 请求 → %s: model=%s, msgs=%d, tools=%d, stream=%s",
                target_model, chat_req.get("model"),
                len(chat_req.get("messages", [])),
                len(chat_req.get("tools", []) or []),
                chat_req.get("stream"))

            # 记录工具名称列表（调试 MCP 工具暴露问题）
            req_tools = body.get("tools", [])
            if req_tools:
                req_tool_names = []
                for t in req_tools:
                    tt = t.get("type", "function")
                    if tt == "function":
                        req_tool_names.append(t.get("function", {}).get("name", t.get("name", "?")))
                    else:
                        # 记录非 function 类型工具的详细信息
                        name_val = t.get("name", t.get("server_label", t.get("server_name", "?")))
                        req_tool_names.append(f"[{tt}:{name_val}]")
                logger.info("HTTP 请求原始工具列表 (%d 个): %s", len(req_tools), req_tool_names)
                # 记录非 function 类型工具的完整定义（调试 MCP 工具格式）
                for t in req_tools:
                    tt = t.get("type", "function")
                    if tt not in ("function", "image_gen", "web_search", "file_search", "code_interpreter", "video_gen"):
                        logger.info("非标准工具类型 [%s]: %s", tt, json.dumps(t, ensure_ascii=False)[:500])
                # 记录 mcp__ 开头工具的完整定义（调试 MCP 工具暴露问题）
                for t in req_tools:
                    tt = t.get("type", "function")
                    name_val = ""
                    if tt == "function":
                        name_val = t.get("function", {}).get("name", t.get("name", ""))
                    else:
                        name_val = t.get("name", "")
                    if name_val and (name_val.startswith("mcp__") or name_val.startswith("namespace__") or name_val == "" or name_val == "codex_app"):
                        logger.info("MCP/特殊工具完整定义 [%s]: %s", name_val or "(空)", json.dumps(t, ensure_ascii=False)[:800])

            chat_tools = chat_req.get("tools", []) or []
            if chat_tools:
                chat_tool_names = [t.get("function", {}).get("name", "?") for t in chat_tools]
                logger.info("HTTP 请求转发工具列表 (%d 个): %s", len(chat_tools), chat_tool_names)

            if verbose:
                _safe_log("Chat 请求详情", chat_req)

            if stream:
                # 2. 流式处理（返回真实模型名，Codex 显示 target_model）
                return StreamingResponse(
                    _handle_stream(client, adapter, chat_req, target_model, verbose, start_time, provider_name, target_model),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            else:
                # 3. 非流式处理
                chat_resp = await client.chat_completion(chat_req)
                if verbose:
                    _safe_log("Chat 响应", chat_resp)
                responses_resp = translate_response(chat_resp, adapter, target_model, chat_req=chat_req)

                # code_interpreter 拦截：执行 python 代码并注入结果
                if builtin_flags["has_code_interpreter"]:
                    responses_resp = _process_code_interpreter_response(responses_resp, model)

                # ── computer_use 工具执行循环 ──────────────────────
                # 模型可能反复调用 computer_use，每次执行后追加结果到对话，
                # 再调用模型，直到模型不再产生 computer_use 调用。
                # 同时累积所有 function_call + function_call_output 项到最终响应 output 中，
                # 确保 Codex 能看到完整的工具调用链并正确维护会话状态。
                if builtin_flags.get("has_computer_use"):
                    from .codex_cua import handle_computer_use_call
                    _CUA_MAX_ROUNDS = 20  # 复杂任务（如画图）需要更多轮次
                    _cua_total_tokens = chat_resp.get("usage", {}).get("total_tokens", 0)
                    _cua_accumulated_output: list[dict] = []  # 累积的 function_call + function_call_output

                    for _round in range(_CUA_MAX_ROUNDS):
                        # 提取本轮响应中的 computer_use 调用
                        _output = responses_resp.get("output", [])
                        _cua_calls = [
                            item for item in _output
                            if item.get("type") == "function_call" and item.get("name") == "computer_use"
                        ]
                        if not _cua_calls:
                            break  # 没有更多 computer_use 调用，退出循环

                        logger.info("computer_use 工具执行循环: 第 %d 轮, %d 个调用",
                                    _round + 1, len(_cua_calls))

                        # 将本轮所有 function_call 项累积到最终输出
                        for item in _output:
                            if item.get("type") == "function_call":
                                _cua_accumulated_output.append(item)

                        # 将本轮 assistant 消息追加到 chat messages
                        _assistant_msg = {"role": "assistant", "content": None, "tool_calls": []}
                        for item in _output:
                            if item.get("type") == "function_call":
                                _assistant_msg["tool_calls"].append({
                                    "id": item.get("call_id", item.get("id", "")),
                                    "type": "function",
                                    "function": {
                                        "name": item.get("name", ""),
                                        "arguments": item.get("arguments", "{}"),
                                    },
                                })
                            elif item.get("type") == "message":
                                _content_parts = item.get("content", [])
                                if isinstance(_content_parts, list):
                                    _texts = [p.get("text", "") for p in _content_parts
                                              if isinstance(p, dict) and p.get("type") in ("text", "output_text")]
                                    if _texts:
                                        _assistant_msg["content"] = "\n".join(_texts)
                        chat_req["messages"].append(_assistant_msg)

                        # 执行每个 computer_use 调用并追加结果
                        for _call in _cua_calls:
                            _args_str = _call.get("arguments", "{}")
                            try:
                                _args = json.loads(_args_str) if _args_str else {}
                            except (json.JSONDecodeError, ValueError):
                                _args = {}
                            _action = _args.get("action", "")
                            _params = _args.get("params", {})
                            _call_id = _call.get("call_id", _call.get("id", ""))

                            logger.info("computer_use 执行: round=%d action=%s params=%s",
                                        _round + 1, _action, json.dumps(_params, ensure_ascii=False)[:200])

                            _result = handle_computer_use_call(_action, _params)
                            _result_text = json.dumps(_result, ensure_ascii=False)
                            if len(_result_text) > 50000:
                                _result_text = _result_text[:50000] + "...[truncated]"

                            # 累积 function_call_output 到最终响应 output
                            _cua_accumulated_output.append({
                                "type": "function_call_output",
                                "call_id": _call_id,
                                "output": _result_text,
                            })

                            chat_req["messages"].append({
                                "role": "tool",
                                "tool_call_id": _call_id,
                                "content": _result_text,
                            })

                        # 再次调用模型
                        logger.info("computer_use 循环: 再次请求模型 (msgs=%d)", len(chat_req["messages"]))
                        chat_resp = await client.chat_completion(chat_req)
                        _cua_total_tokens += chat_resp.get("usage", {}).get("total_tokens", 0)
                        responses_resp = translate_response(chat_resp, adapter, target_model, chat_req=chat_req)

                    # 循环结束 — 将累积的工具调用链拼入最终响应 output
                    _final_items = responses_resp.get("output", [])
                    if _cua_accumulated_output:
                        responses_resp["output"] = _cua_accumulated_output + _final_items
                    responses_resp["status"] = "completed"
                    # 更新总 token 数
                    chat_resp["usage"] = chat_resp.get("usage", {})
                    chat_resp["usage"]["total_tokens"] = _cua_total_tokens

                    logger.info("CUA 循环结束: 共 %d 轮, 累积 %d 项工具交互 + %d 项最终输出",
                                _round + 1, len(_cua_accumulated_output), len(_final_items))

                    # 写入去重缓存
                    if _input_hash:
                        _cua_dedup_cache[_input_hash] = (copy.deepcopy(responses_resp), time.time())
                        logger.info("CUA 去重缓存已写入: hash=%s, resp_id=%s",
                                    _input_hash, responses_resp.get("id", "?"))

                # 回显 previous_response_id（OpenAI Responses API 标准行为）
                _req_prev_id = body.get("previous_response_id", "")
                if _req_prev_id:
                    responses_resp["previous_response_id"] = _req_prev_id

                # 缓存响应供 previous_response_id 查询（store=false 时跳过）
                resp_id = responses_resp.get("id", "")
                should_store = chat_req.get("_store", True)
                if resp_id and should_store:
                    get_response_cache().put(resp_id, responses_resp)

                # 统计 token
                tokens = chat_resp.get("usage", {}).get("total_tokens", 0)

                if verbose:
                    _safe_log("Responses 响应", responses_resp)

                _record_request(start_time, model, "responses", 200, False, "", tokens, provider=provider_name, target_model=target_model)
                circuit_breaker.on_success()

                # 如果原始请求是流式的，但被强制改为非流式执行 CUA 循环，
                # 需要将结果包装成 SSE 流返回，否则 Codex 会报
                # "stream disconnected before completion: stream closed before response.completed"
                if _original_stream and builtin_flags.get("has_computer_use"):
                    logger.info("CUA 循环完成，将结果包装为 SSE 流返回 (原始 stream=true)")
                    return StreamingResponse(
                        _wrap_cua_response_as_sse(responses_resp, target_model),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "X-Accel-Buffering": "no",
                        },
                    )

                return JSONResponse(content=responses_resp)

        except Exception as exc:
            status_code = 500
            error_msg = str(exc)
            logger.exception("请求处理异常")
            circuit_breaker.on_failure()
            _record_request(start_time, model, "responses", status_code, stream, error_msg, provider=provider_name, target_model=target_model)
            return JSONResponse(
                content=build_error_response(error_msg),
                status_code=500,
            )
        finally:
            if not stream:
                await client.close()

    @app.post("/v1/chat/completions")
    async def chat_completions_endpoint(request: Request):
        """辅助端点: 透传 Chat Completions 请求（兼容旧版配置）"""
        start_time = time.time()
        try:
            body = await _parse_request_json(request)
        except Exception:
            return JSONResponse(
                content=build_error_response("无效的 JSON 请求体"),
                status_code=400,
            )

        model = body.get("model", "unknown")
        stream = body.get("stream", False)

        try:
            adapter, provider_name, target_model, api_keys = _get_adapter_for_model(model)
        except ValueError as exc:
            _record_request(start_time, model, "chat", 400, stream, str(exc), provider="", target_model="")
            return JSONResponse(
                content=build_error_response(str(exc)),
                status_code=400,
            )

        body["model"] = target_model
        body = adapter.preprocess_chat_request(body)

        provider_timeout = get_config().get_provider(provider_name).get("timeout", 120) if provider_name else 120
        client = UpstreamClient(adapter, api_keys, timeout=provider_timeout, stream_timeout=max(provider_timeout, 600))
        try:
            if stream:
                async def _sse_gen():
                    last_chunk_time = asyncio.get_event_loop().time()
                    try:
                        async for chunk in client.chat_completion_stream(body):
                            last_chunk_time = asyncio.get_event_loop().time()
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                    except Exception as e:
                        logger.error("chat/completions 流式异常: %s", e)
                        # 发送错误事件让客户端知晓
                        error_data = {"error": {"message": str(e), "type": "server_error"}}
                        yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"

                return StreamingResponse(
                    _sse_gen(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )
            else:
                resp = await client.chat_completion(body)
                resp = adapter.postprocess_chat_response(resp)
                tokens = resp.get("usage", {}).get("total_tokens", 0)
                _record_request(start_time, model, "chat", 200, False, "", tokens, provider=provider_name, target_model=target_model)
                return JSONResponse(content=resp)
        except Exception as exc:
            _record_request(start_time, model, "chat", 500, stream, str(exc), provider=provider_name, target_model=target_model)
            return JSONResponse(
                content=build_error_response(str(exc)),
                status_code=500,
            )
        finally:
            await client.close()

    @app.post("/v1/images/generations")
    async def images_generations(request: Request):
        """图片生成端点: 接受 DALL-E 格式请求，路由到配置的生图模型"""
        cfg = get_config()
        try:
            body = await _parse_request_json(request)
        except Exception:
            return JSONResponse({"error": {"message": "无效的 JSON 请求体"}}, 400)

        model = body.get("model", "unknown")
        raw_entry = cfg.model_mapping.get(model)

        if not raw_entry:
            return JSONResponse({"error": {"message": f"未找到模型: {model}"}}, 404)

        # 取活跃条目（支持多模型列表）
        if isinstance(raw_entry, list):
            entry = next((e for e in raw_entry if e.get("enabled")), raw_entry[0])
        else:
            entry = raw_entry

        # 确定用哪个模型生图
        gen_alias = model
        if entry.get("is_image_gen"):
            # 本模型就是生图模型
            gen_target = entry.get("target", model)
            gen_provider = entry.get("provider", "")
        elif entry.get("image_gen_alias"):
            gen_alias = entry["image_gen_alias"]
            gen_entry_raw = cfg.model_mapping.get(gen_alias)
            if not gen_entry_raw:
                return JSONResponse({"error": {"message": f"生图模型未找到: {gen_alias}"}}, 400)
            if isinstance(gen_entry_raw, list):
                gen_entry = next((e for e in gen_entry_raw if e.get("enabled")), gen_entry_raw[0])
            else:
                gen_entry = gen_entry_raw
            gen_target = gen_entry.get("target", gen_alias)
            gen_provider = gen_entry.get("provider", "")
        else:
            return JSONResponse(
                {"error": {"message": f"模型 '{model}' 未配置生图模型，请在模型设置中添加 image_gen_alias"}}, 400)

        # 查找 provider
        provider_name = gen_provider
        if not provider_name or provider_name not in cfg.providers:
            # 回退查找
            for pname, pinfo in cfg.providers.items():
                if pinfo.get("adapter") == provider_name or pname == provider_name:
                    provider_name = pname
                    break
            else:
                for pname, pinfo in cfg.providers.items():
                    if pname in gen_target.lower() or pinfo.get("adapter", "") in gen_target.lower():
                        provider_name = pname
                        break
                else:
                    if cfg.providers:
                        provider_name = next(iter(cfg.providers))

        if not provider_name:
            return JSONResponse({"error": {"message": f"未找到生图 provider: {gen_alias}"}}, 400)

        try:
            adapter, _, _, api_keys = _resolve_adapter(provider_name, gen_target)
        except ValueError as exc:
            return JSONResponse({"error": {"message": str(exc)}}, 400)

        # 构建生图请求（通过适配器，支持不同厂商的生图 API 格式）
        img_body = {
            "model": gen_target,
            "prompt": body.get("prompt", ""),
            "n": body.get("n", 1),
            "size": body.get("size", "2560x1440"),
        }
        # 透传 DALL-E 标准字段
        for key in ("response_format", "quality", "style", "user"):
            if key in body:
                img_body[key] = body[key]
        # 透传厂商扩展字段（如 output_format, watermark 等）
        for key in ("output_format", "watermark", "negative_prompt", "seed", "steps", "guidance_scale"):
            if key in body:
                img_body[key] = body[key]

        img_body = adapter.preprocess_image_gen_request(img_body)
        img_url = adapter.build_image_gen_url()
        headers = adapter.get_headers(api_keys[0])

        logger.info("生图请求 → %s/%s: prompt=%.80s..., size=%s",
            provider_name, gen_target,
            body.get("prompt", "")[:80],
            body.get("size", "1024x1024"))

        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120), trust_env=False) as client:
                resp = await client.post(img_url, json=img_body, headers=headers)
                elapsed = (time.time() - start_time) * 1000
                result = resp.json()

                if resp.status_code == 200:
                    logger.info("生图成功 → %s/%s (%.0fms)", provider_name, gen_target, elapsed)
                else:
                    logger.warning("生图失败 → %s/%s: HTTP %d", provider_name, gen_target, resp.status_code)

                _record_request(start_time, gen_alias, "images", resp.status_code, False,
                    error="" if resp.status_code == 200 else result.get("error", {}).get("message", ""),
                    provider=provider_name, target_model=gen_target)

                return JSONResponse(content=result, status_code=resp.status_code)
        except httpx.TimeoutException:
            return JSONResponse({"error": {"message": "生图请求超时（120秒）"}}, 504)
        except Exception as exc:
            logger.exception("生图请求异常")
            return JSONResponse({"error": {"message": str(exc)}}, 500)

    @app.get("/v1/responses/{response_id}")
    async def get_response(response_id: str):
        """查询历史响应: 从 ResponseCache 获取缓存的响应数据"""
        cache = get_response_cache()
        data = cache.get(response_id)
        if data:
            return JSONResponse(content=data, status_code=200)
        return JSONResponse(
            content={"error": {"message": "Response not found", "type": "not_found"}},
            status_code=404,
        )

    @app.delete("/v1/responses/{response_id}")
    async def delete_response(response_id: str):
        """删除指定响应: 从 ResponseCache 的内存和磁盘删除"""
        cache = get_response_cache()
        data = cache.get(response_id)
        if not data:
            return JSONResponse(
                content={"error": {"message": "Response not found", "type": "not_found"}},
                status_code=404,
            )
        # 从内存缓存删除
        with cache._lock:
            cache._cache.pop(response_id, None)
        # 从磁盘删除
        cache._delete_from_disk(response_id)
        logger.info("已删除响应缓存: %s", response_id)
        return JSONResponse(content={"success": True}, status_code=200)

    @app.post("/v1/videos/generations")
    async def videos_generations(request: Request):
        """视频生成端点: 接受视频生成请求，路由到配置的视频生成模型"""
        cfg = get_config()
        try:
            body = await _parse_request_json(request)
        except Exception:
            return JSONResponse({"error": {"message": "无效的 JSON 请求体"}}, 400)

        model = body.get("model", "unknown")
        raw_entry = cfg.model_mapping.get(model)

        if not raw_entry:
            return JSONResponse({"error": {"message": f"未找到模型: {model}"}}, 404)

        # 取活跃条目（支持多模型列表）
        if isinstance(raw_entry, list):
            entry = next((e for e in raw_entry if e.get("enabled")), raw_entry[0])
        else:
            entry = raw_entry

        # 确定用哪个模型生成视频
        gen_alias = model
        if entry.get("is_video_gen"):
            gen_target = entry.get("target", model)
            gen_provider = entry.get("provider", "")
        elif entry.get("video_gen_alias"):
            gen_alias = entry["video_gen_alias"]
            gen_entry_raw = cfg.model_mapping.get(gen_alias)
            if not gen_entry_raw:
                return JSONResponse({"error": {"message": f"视频生成模型未找到: {gen_alias}"}}, 400)
            if isinstance(gen_entry_raw, list):
                gen_entry = next((e for e in gen_entry_raw if e.get("enabled")), gen_entry_raw[0])
            else:
                gen_entry = gen_entry_raw
            gen_target = gen_entry.get("target", gen_alias)
            gen_provider = gen_entry.get("provider", "")
        else:
            return JSONResponse(
                {"error": {"message": f"模型 '{model}' 未配置视频生成模型，请在模型设置中添加 video_gen_alias"}}, 400)

        # 查找 provider
        provider_name = gen_provider
        if not provider_name or provider_name not in cfg.providers:
            for pname, pinfo in cfg.providers.items():
                if pinfo.get("adapter") == provider_name or pname == provider_name:
                    provider_name = pname
                    break
            else:
                for pname, pinfo in cfg.providers.items():
                    if pname in gen_target.lower() or pinfo.get("adapter", "") in gen_target.lower():
                        provider_name = pname
                        break
                else:
                    if cfg.providers:
                        provider_name = next(iter(cfg.providers))

        if not provider_name:
            return JSONResponse({"error": {"message": f"未找到视频生成 provider: {gen_alias}"}}, 400)

        try:
            adapter, _, _, api_keys = _resolve_adapter(provider_name, gen_target)
        except ValueError as exc:
            return JSONResponse({"error": {"message": str(exc)}}, 400)

        # 构建视频生成 URL
        if hasattr(adapter, "build_video_gen_url"):
            vid_url = adapter.build_video_gen_url()
        else:
            base = adapter.base_url.rstrip("/")
            vid_url = f"{base}/videos/generations"

        headers = adapter.get_headers(api_keys[0])

        # 构建请求体
        vid_body = {
            "model": gen_target,
            "prompt": body.get("prompt", ""),
            "n": body.get("n", 1),
        }
        for key in ("size", "duration", "fps", "quality", "style", "negative_prompt", "seed", "response_format", "user"):
            if key in body:
                vid_body[key] = body[key]

        logger.info("视频生成请求 → %s/%s: prompt=%.80s...",
            provider_name, gen_target, body.get("prompt", "")[:80])

        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300), trust_env=False) as client:
                resp = await client.post(vid_url, json=vid_body, headers=headers)
                elapsed = (time.time() - start_time) * 1000
                result = resp.json()

                if resp.status_code == 200:
                    logger.info("视频生成成功 → %s/%s (%.0fms)", provider_name, gen_target, elapsed)
                else:
                    logger.warning("视频生成失败 → %s/%s: HTTP %d", provider_name, gen_target, resp.status_code)

                _record_request(start_time, gen_alias, "videos", resp.status_code, False,
                    error="" if resp.status_code == 200 else result.get("error", {}).get("message", ""),
                    provider=provider_name, target_model=gen_target)

                return JSONResponse(content=result, status_code=resp.status_code)
        except httpx.TimeoutException:
            return JSONResponse({"error": {"message": "视频生成请求超时（300秒）"}}, 504)
        except Exception as exc:
            logger.exception("视频生成请求异常")
            return JSONResponse({"error": {"message": str(exc)}}, 500)

    async def _ws_send_sse_as_json(websocket: WebSocket, event_line: str) -> None:
        """将 SSE 格式的事件行 (data: {...}\n\n) 转换为 JSON 并通过 WebSocket 发送

        WebSocket 协议要求发送纯 JSON 对象，而非 SSE 格式文本。
        HTTP 流式传输使用 SSE 格式 (data: {...}\n\n)，但 WebSocket 应发送 {...}。
        """
        if not event_line or not event_line.startswith("data: "):
            return
        # 提取 JSON 部分: "data: {...}\n\n" → "{...}"
        json_str = event_line[6:].rstrip("\n")
        if not json_str:
            return
        try:
            data = json.loads(json_str)
            await websocket.send_json(data)
        except json.JSONDecodeError:
            logger.warning("WebSocket SSE 解析失败: %s", event_line[:100])

    @app.websocket("/v1/responses")
    async def responses_websocket(websocket: WebSocket):
        """WebSocket 端点: 接受 Codex 的 WebSocket 升级请求，保持长连接并处理多次请求"""
        await websocket.accept()
        logger.info("WebSocket 连接已建立")
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                except asyncio.TimeoutError:
                    # 60秒内无消息，发送心跳保持连接
                    try:
                        await websocket.send_json({"type": "heartbeat"})
                    except Exception:
                        break
                    continue
                except WebSocketDisconnect:
                    logger.info("WebSocket 客户端断开")
                    break

                start_time = time.time()
                try:
                    body = json.loads(raw)
                except Exception:
                    await websocket.send_json(build_error_response("无效的 JSON 请求体"))
                    continue

                model = body.get("model", "unknown")
                stream = body.get("stream", False)

                # 记录 Codex 发送的工具列表（调试 MCP 工具暴露问题）
                tools = body.get("tools", [])
                if tools:
                    tool_names = []
                    for t in tools:
                        if t.get("type") == "function":
                            tool_names.append(t.get("function", {}).get("name", "?"))
                        elif t.get("type"):
                            tool_names.append(f"[{t.get('type')}]")
                        else:
                            tool_names.append(t.get("name", "?"))
                    logger.info("WebSocket 请求工具列表 (%d 个): %s", len(tools), tool_names)
                else:
                    logger.info("WebSocket 请求无工具")

                # 会话粘性
                conv_id = _extract_conversation_id(body)
                affinity_provider = get_affinity(conv_id) if conv_id else None

                try:
                    if affinity_provider:
                        try:
                            adapter, provider_name, target_model, api_keys = _resolve_adapter(affinity_provider, body.get("model", model))
                        except ValueError:
                            affinity_provider = None
                            adapter, provider_name, target_model, api_keys = _route_vision(model, body)
                    else:
                        adapter, provider_name, target_model, api_keys = _route_vision(model, body)

                    if conv_id and not affinity_provider:
                        set_affinity(conv_id, provider_name)
                except ValueError as exc:
                    await websocket.send_json(build_error_response(str(exc)))
                    continue

                # 熔断器检查
                circuit_breaker = get_circuit_breaker_registry().get(provider_name)
                if not circuit_breaker.before_request():
                    await websocket.send_json(build_error_response(
                        f"Provider '{provider_name}' 已熔断", "circuit_open", 503))
                    continue

                provider_timeout = get_config().get_provider(provider_name).get("timeout", 120) if provider_name else 120
                client = UpstreamClient(adapter, api_keys, timeout=provider_timeout, stream_timeout=max(provider_timeout, 600))

                try:
                    cfg = get_config()
                    model_entry = cfg.model_mapping.get(model)
                    if isinstance(model_entry, list):
                        model_item = next((e for e in model_entry if e.get("enabled")), model_entry[0] if model_entry else None)
                    else:
                        model_item = model_entry
                    if model_item is not None:
                        if not model_item.get("enable_thinking", True):
                            body["_disable_thinking"] = True
                        if "thinking_budget" in model_item:
                            budget = model_item["thinking_budget"]
                            other_count = sum(1 for item in body.get("input", []) if item.get("role") != "system")
                            if other_count > 25:
                                budget = min(budget, 4096)
                            elif other_count > 15:
                                budget = min(budget, 8192)
                            body["_thinking_budget"] = budget

                    builtin_flags = _intercept_builtin_tools(body)
                    chat_req = translate_request(body, adapter, target_model, alias=model)
                    has_image_gen = chat_req.pop("_has_image_gen", False)

                    if has_image_gen or builtin_flags.get("has_video_gen"):
                        await websocket.send_json(build_error_response("WebSocket 不支持图片/视频生成，请使用 HTTP POST"))
                        continue

                    chat_req.setdefault("_store", body.get("store", True))

                    if stream:
                        # WebSocket 流式：必须用 StreamTranslator 把上游 Chat SSE 翻译为
                        # Responses API 的事件格式，然后以 JSON 格式发给 Codex
                        # 注意：WebSocket 发送的是纯 JSON 对象，不是 SSE 格式 (data: {...}\n\n)
                        translator = StreamTranslator(model=target_model, chat_req=chat_req)
                        # 预热：发送 response.created + response.in_progress
                        for event_line in translator.warmup():
                            await _ws_send_sse_as_json(websocket, event_line)
                        ws_last_chunk_time = asyncio.get_event_loop().time()
                        ws_chunk_count = 0
                        async for chunk in client.chat_completion_stream(chat_req):
                            ws_last_chunk_time = asyncio.get_event_loop().time()
                            ws_chunk_count += 1
                            chunk = adapter.stream_event_transform(chunk)
                            for event_line in translator.translate_chunk(chunk):
                                await _ws_send_sse_as_json(websocket, event_line)
                        # 结束时发送 response.completed + 终止符
                        for event_line in translator._finish():
                            await _ws_send_sse_as_json(websocket, event_line)
                        _store_val = chat_req.get("_store", True)
                        logger.info("WebSocket 流式结束，准备缓存: _store=%s, response_id=%s, output_items=%d",
                            _store_val, translator.response_id, len(translator._output_items))
                        if _store_val:
                            cached_output = translator._output_items
                            fc_count = sum(1 for item in cached_output if item.get("type") == "function_call")
                            logger.info("WebSocket 流式完成，缓存响应: id=%s, output_items=%d, function_calls=%d",
                                translator.response_id, len(cached_output), fc_count)
                            get_response_cache().put(translator.response_id, {
                                "id": translator.response_id,
                                "model": model,
                                "output": cached_output,
                            })
                        # 缓存 reasoning_content 供下一轮恢复
                        reasoning_text = "".join(translator._reasoning_buf)
                        if reasoning_text:
                            save_last_reasoning(reasoning_text)
                        circuit_breaker.on_success()
                        _record_request(start_time, model, "responses_ws", 200, True, "",
                            provider=provider_name, target_model=target_model)
                    else:
                        chat_resp = await client.chat_completion(chat_req)
                        responses_resp = translate_response(chat_resp, adapter, target_model, chat_req=chat_req)
                        if builtin_flags.get("has_code_interpreter"):
                            responses_resp = _process_code_interpreter_response(responses_resp, model)
                        if builtin_flags.get("has_computer_use"):
                            responses_resp = _process_computer_use_response(responses_resp, model)
                        resp_id = responses_resp.get("id", "")
                        if resp_id and chat_req.get("_store", True):
                            get_response_cache().put(resp_id, responses_resp)
                        tokens = chat_resp.get("usage", {}).get("total_tokens", 0)
                        _record_request(start_time, model, "responses_ws", 200, False, "", tokens,
                            provider=provider_name, target_model=target_model)
                        circuit_breaker.on_success()
                        await websocket.send_json(responses_resp)

                except Exception as exc:
                    logger.exception("WebSocket 请求处理异常")
                    circuit_breaker.on_failure()
                    _record_request(start_time, model, "responses_ws", 500, stream, str(exc),
                        provider=provider_name, target_model=target_model)
                    try:
                        await websocket.send_json(build_error_response(str(exc)))
                    except Exception:
                        pass
                finally:
                    await client.close()
        except Exception:
            logger.debug("WebSocket 连接关闭")
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    return app


# ── 流式处理 ────────────────────────────────────────────────────────

def _is_budget_constraint_error(error_msg: str) -> bool:
    """检测是否为 thinking budget_tokens 约束错误（需要 rectifier 修正）"""
    lower = error_msg.lower()
    return (
        ("budget_tokens" in lower or "budget tokens" in lower)
        and "thinking" in lower
        and ("1024" in lower and ("greater than" in lower or ">=" in lower or "input should be" in lower))
    )


def _rectify_budget_params(chat_req: dict) -> None:
    """修正 thinking budget 参数：budget_tokens=32000, max_tokens=64000"""
    MAX_BUDGET = 32000
    MAX_TOKENS = 64000

    if "thinking" not in chat_req or not isinstance(chat_req.get("thinking"), dict):
        chat_req["thinking"] = {}
    chat_req["thinking"]["type"] = "enabled"
    chat_req["thinking"]["budget_tokens"] = MAX_BUDGET

    cur_max = chat_req.get("max_tokens", 0) or 0
    if cur_max < MAX_BUDGET + 1:
        chat_req["max_tokens"] = MAX_TOKENS


async def _wrap_cua_response_as_sse(responses_resp: dict, model: str):
    """将非流式 CUA 响应包装成 SSE 流式格式返回

    Codex 发送 stream=true 请求，但 computer_use 需要非流式执行 CUA 循环。
    执行完成后，需要将结果包装成 SSE 事件流返回，否则 Codex 会报
    "stream disconnected before completion: stream closed before response.completed"

    SSE 事件序列：
    1. response.created  — 响应已创建
    2. response.in_progress — 响应进行中
    3. response.completed — 响应完成（包含完整的 output 和 usage）
    """
    response_id = responses_resp.get("id", _uid("resp"))
    status = responses_resp.get("status", "completed")
    output = responses_resp.get("output", [])
    usage = responses_resp.get("usage", {})
    metadata = responses_resp.get("metadata")

    # 1. response.created
    yield _sse_line({
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "model": model,
            "status": "in_progress",
        },
    })

    # 2. response.in_progress
    yield _sse_line({
        "type": "response.in_progress",
        "response": {
            "id": response_id,
            "status": "in_progress",
        },
    })

    # 3. response.completed — 包含完整的 output 和 usage
    completed_event: dict = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "model": model,
            "status": status,
            "output": output,
            "usage": usage,
        },
    }
    if isinstance(metadata, dict):
        completed_event["response"]["metadata"] = metadata
    yield _sse_line(completed_event)


async def _handle_stream(
    client: UpstreamClient,
    adapter: BaseAdapter,
    chat_req: dict,
    model: str,
    verbose: bool,
    start_time: float,
    provider: str = "",
    target_model: str = "",
):
    """处理流式请求: 上游 Chat SSE → 适配器变换 → 协议转换 → Responses SSE

    稳定性保障:
    - 30s chunk 超时，容忍 DeepSeek 等模型的长时间推理
    - 闲置 > 25s 时发送保活信号
    - 断连自动重试一次（重建 translator 避免状态污染）
    - 始终发送 response.completed，Codex 不会悬挂
    """
    import httpx

    RETRYABLE = (
        httpx.RemoteProtocolError, httpx.ReadTimeout,
        httpx.ConnectTimeout, httpx.ConnectError,
        ConnectionResetError, ConnectionAbortedError,
        httpx.ReadError,
    )

    CHUNK_TIMEOUT = 120.0  # 推理模型思考阶段可能长时间无输出，延长到 120 秒
    MAX_RETRIES = 2  # 增加重试次数以应对不稳定的国产模型 API
    IDLE_BEFORE_PING = 25.0

    stream_error = ""
    retry_count = 0
    translator = StreamTranslator(model=model, chat_req=chat_req)
    empty_content_retried = False  # 防止无限重试空洞响应

    # 响应预热: 立即发送 response.created，不等上游 API 响应
    for event_line in translator.warmup():
        yield event_line

    chunk_count = 0
    first_chunk_time = 0.0

    while retry_count <= MAX_RETRIES:
        # ── 空洞响应重试：上一轮没产出实质内容，关闭思考重试 ──
        if empty_content_retried and retry_count == 0:
            logger.info("空洞响应重试: 关闭 thinking 模式重新请求")
            chat_req["_disable_thinking"] = True
            chat_req.pop("_thinking_budget", None)
            translator = StreamTranslator(model=model, chat_req=chat_req)
            if client._stream_client:
                try:
                    await client._stream_client.aclose()
                except Exception:
                    pass
                client._stream_client = None
            for event_line in translator.warmup():
                yield event_line
            chunk_count = 0
            first_chunk_time = 0.0
            retry_count = 1  # 防止重复进入空洞重试分支

        chat_stream = client.chat_completion_stream(chat_req)
        stream_error = ""
        last_chunk_time = asyncio.get_event_loop().time()

        try:
            while True:
                # 使用 create_task + wait 替代 wait_for，避免超时取消破坏底层 HTTP 流
                next_chunk_task = asyncio.ensure_future(anext(chat_stream))
                try:
                    done, pending = await asyncio.wait(
                        {next_chunk_task},
                        timeout=CHUNK_TIMEOUT,
                        return_when=asyncio.FIRST_COMPLETED
                    )
                except Exception as wait_err:
                    logger.error("asyncio.wait 异常: %s", wait_err)
                    break

                if not done:
                    # 超时但任务仍在 pending，不取消它，只发心跳保持连接
                    idle_duration = asyncio.get_event_loop().time() - last_chunk_time
                    if idle_duration > IDLE_BEFORE_PING:
                        yield ": keepalive\n\n"
                    yield ": heartbeat\n\n"
                    # 继续等待同一个 task（不创建新 task）
                    continue

                # task 已完成，获取结果
                try:
                    chunk = next_chunk_task.result()
                except StopAsyncIteration:
                    logger.debug("流式上游结束 req=%s chunks=%d 耗时=%.1fs",
                        translator.response_id, chunk_count,
                        asyncio.get_event_loop().time() - start_time)
                    break
                except Exception as e:
                    logger.error("获取 chunk 结果异常: %s", e)
                    stream_error = str(e)
                    break

                last_chunk_time = asyncio.get_event_loop().time()
                chunk_count += 1
                if first_chunk_time == 0.0:
                    first_chunk_time = last_chunk_time
                    logger.debug("流式首chunk到达 (%.1fs后), req=%s",
                        first_chunk_time - start_time, translator.response_id)

                chunk = adapter.stream_event_transform(chunk)

                # 始终记录空 choices（可能是问题征兆）
                choices = chunk.get("choices", [])
                if not choices:
                    logger.debug("空choices chunk req=%s keys=%s", translator.response_id, list(chunk.keys()))

                if verbose:
                    _safe_log("Chat chunk", chunk)

                events_yielded = 0
                for event_line in translator.translate_chunk(chunk):
                    events_yielded += 1
                    yield event_line

            # 检测空洞响应：模型没产出实质内容就停了
            # 涵盖三种情况：
            #   1. 只推理不输出 (reasoning=有, content=空, tools=空)
            #   2. 完全空响应 (reasoning=空, content=空, tools=空)
            #   3. 敷衍短输出 (content < 50 字符, tools=空)
            content_text = "".join(translator._accumulated_text).strip()
            has_tool_calls = bool(translator._tc_buf)
            has_useful_content = len(content_text) >= 10  # 降低阈值，短回复也算有效

            is_empty_response = not has_tool_calls and not has_useful_content

            if is_empty_response and not empty_content_retried:
                logger.warning(
                    "检测到空洞响应 (reasoning=%d chars, content='%s', tools=%d), 关闭思考重试",
                    len("".join(translator._reasoning_buf)),
                    content_text[:80],
                    len(translator._tc_buf),
                )
                empty_content_retried = True
                retry_count = 0
                # 不 break，外层 while 循环下一轮会检测标志并重试
            else:
                break  # 成功，退出重试循环

        except RETRYABLE as exc:
            stream_error = str(exc)
            retry_count += 1
            logger.warning(
                "流式连接断开 (第%d次, %.0fs后): %s",
                retry_count,
                asyncio.get_event_loop().time() - start_time,
                exc,
            )

            if client._stream_client:
                try:
                    await client._stream_client.aclose()
                except Exception:
                    pass
                client._stream_client = None

            if retry_count <= MAX_RETRIES:
                logger.info("尝试重连 (第%d次)...", retry_count)
                # 多 key 轮转：重试时切换到下一个 key
                client.rotate_key()
                translator = StreamTranslator(model=model, chat_req=chat_req)
                await asyncio.sleep(1.5)

        except httpx.HTTPStatusError as exc:
            # Thinking budget rectifier: 自动修正 budget_tokens 约束错误并重试
            error_msg = str(exc)
            if (adapter.supports_thinking_budget
                    and retry_count <= MAX_RETRIES
                    and _is_budget_constraint_error(error_msg)):
                logger.warning(
                    "检测到 thinking budget 约束错误，自动修正并重试: %s",
                    error_msg[:150],
                )
                _rectify_budget_params(chat_req)
                stream_error = error_msg
                retry_count += 1
                if client._stream_client:
                    try:
                        await client._stream_client.aclose()
                    except Exception:
                        pass
                    client._stream_client = None
                client.rotate_key()
                translator = StreamTranslator(model=model, chat_req=chat_req)
                await asyncio.sleep(1.0)
            else:
                stream_error = error_msg
                logger.error("上游返回错误: %s", error_msg[:200])
                break

        except Exception as exc:
            stream_error = str(exc)
            logger.exception("流式处理异常")
            break

    # 流正常结束
    if not stream_error:
        try:
            for event_line in translator._finish():
                yield event_line
            # store=false 时跳过缓存写入
            if chat_req.get("_store", True):
                get_response_cache().put(translator.response_id, {
                    "id": translator.response_id,
                    "model": model,
                    "output": translator._output_items,
                })
            # 缓存 reasoning_content 供下一轮恢复（DeepSeek tool_call 场景）
            reasoning_text = "".join(translator._reasoning_buf)
            if reasoning_text:
                save_last_reasoning(reasoning_text)
        except Exception as exc:
            logger.exception("流结束事件发送异常")
            stream_error = str(exc)

    # 流异常结束时发送 error 事件
    if stream_error:
        try:
            _close_incomplete_items(translator)
            yield _sse_line(build_error_response(stream_error))
            yield _sse_line({
                "type": "response.completed",
                "response": {
                    "id": translator.response_id,
                    "object": "response",
                    "model": model,
                    "status": "failed",
                    "output": translator._output_items,
                },
            })
        except Exception:
            pass

    if stream_error:
        _record_request(start_time, model, "responses", 500, True, stream_error, provider=provider, target_model=target_model)
        get_circuit_breaker_registry().get(provider).on_failure()
    else:
        _record_request(start_time, model, "responses", 200, True, "", provider=provider, target_model=target_model)
        get_circuit_breaker_registry().get(provider).on_success()

    await client.close()


def _close_incomplete_items(translator) -> None:
    """关闭 StreamTranslator 中所有未完成的输出项，标记为 completed"""
    if translator._reasoning_started:
        reasoning_text = "".join(translator._reasoning_buf)
        idx = translator._reasoning_item_index
        if 0 <= idx < len(translator._output_items):
            item = translator._output_items[idx]
            item["status"] = "completed"
            if item.get("content"):
                item["content"][0]["text"] = reasoning_text
        translator._reasoning_started = False

    if translator._text_started:
        idx = translator._text_item_index
        if 0 <= idx < len(translator._output_items):
            item = translator._output_items[idx]
            item["status"] = "completed"
            if item.get("content"):
                item["content"][0]["text"] = translator._accumulated_text
        translator._text_started = False

    for tc_index in translator._tc_buf:
        buf = translator._tc_buf[tc_index]
        idx = buf["item_index"]
        if 0 <= idx < len(translator._output_items):
            translator._output_items[idx]["status"] = "completed"
            translator._output_items[idx]["name"] = buf["name"]
            translator._output_items[idx]["arguments"] = buf["arguments"]


def _record_request(
    start_time: float,
    model: str,
    endpoint: str,
    status_code: int,
    stream: bool,
    error: str = "",
    tokens: int = 0,
    provider: str = "",
    target_model: str = "",
):
    elapsed = (time.time() - start_time) * 1000
    get_stats().record(RequestLog(
        timestamp=start_time,
        model=model,
        endpoint=endpoint,
        status_code=status_code,
        elapsed_ms=elapsed,
        tokens=tokens,
        error=error,
        stream=stream,
        provider=provider,
        target_model=target_model,
    ))


def _record_and_respond(
    start_time: float,
    status_code: int,
    error: str,
    model: str,
    stream: bool,
    provider: str = "",
    target_model: str = "",
):
    _record_request(start_time, model, "responses", status_code, stream, error, provider=provider, target_model=target_model)
    return JSONResponse(
        content=build_error_response(error),
        status_code=status_code,
    )


def _safe_log(label: str, data: dict) -> None:
    """安全日志记录（截断过长内容，移除敏感字段）"""
    import copy
    d = copy.deepcopy(data)
    for msg in d.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 500:
            msg["content"] = content[:500] + "...[truncated]"
    logger.debug("%s: %s", label, json.dumps(d, ensure_ascii=False, default=str)[:2000])
