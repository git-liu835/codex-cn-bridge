"""FastAPI 服务器 —— 提供 /v1/responses 端点、管理 API 和 WebSocket"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import get_config, reload_config
from .adapters import get_registry
from .adapters.base import BaseAdapter
from .protocol import translate_request, translate_response, StreamTranslator, _sse_line
from .client import UpstreamClient
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


def _setup_logging(verbose: bool = False) -> None:
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


def _get_adapter_for_model(model: str) -> tuple[BaseAdapter, str, str, str]:
    """根据 code 模型名查找适配器

    Returns:
        (adapter, provider_name, target_model, api_key)
    """
    cfg = get_config()
    provider_name, target_model = cfg.resolve_model(model)
    return _resolve_adapter(provider_name, target_model)


def _resolve_adapter(provider_name: str, target_model: str) -> tuple[BaseAdapter, str, str, str]:
    """根据 provider 名和目标模型查找适配器实例"""
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

    api_key = provider.get("api_key", "")
    if not api_key:
        raise ValueError(
            f"Provider '{provider_name}' 的 API Key 未设置。"
            f"请设置环境变量 {provider.get('api_key_env', '???')}"
        )

    # 允许 provider 覆盖 base_url
    if provider.get("base_url"):
        adapter.base_url = provider["base_url"]

    return adapter, provider_name, target_model, api_key


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
        adapter, _, _, api_key = _resolve_adapter(provider_name, img_target)
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
    headers = adapter.get_headers(api_key)

    logger.info("image_gen 拦截 → 调用生图 API: %s, prompt=%.80s..., size=%s",
        img_url, prompt[:80], size)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
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
                async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
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
        yield
        logger.info("code CN Bridge 已关闭")

    app = FastAPI(
        title="code CN Bridge",
        version="0.3.19",
        description="OpenAI Responses API → Chat Completions API 协议转换代理",
        lifespan=lifespan,
    )

    app.add_middleware(ErrorHandlingMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(DetailedLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "app://.", "file://", "null"],
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
        cfg = get_config()
        models = []
        for alias, entry in cfg.model_mapping.items():
            # 支持多模型列表：至少一个条目启用就算启用
            items = entry if isinstance(entry, list) else [entry]
            if not any(item.get("enabled", True) for item in items):
                continue
            provider = ""
            for item in items:
                if item.get("enabled", True):
                    provider = item.get("provider", "code-cn-bridge")
                    break
            models.append({
                "id": alias,
                "object": "model",
                "created": 1700000000,
                "owned_by": provider,
            })
        return {"object": "list", "data": models}

    @app.post("/admin/reload-config")
    async def admin_reload():
        reload_config()
        return {"status": "ok", "message": "配置已重新加载"}

    @app.post("/v1/responses")
    async def responses_endpoint(request: Request):
        """核心端点: 接受 Responses API 请求，返回 Responses API 响应"""
        start_time = time.time()
        status_code = 200
        error_msg = ""

        try:
            body = await request.json()
        except Exception:
            return _record_and_respond(
                start_time, status_code=400, error="无效的 JSON 请求体",
                model="unknown", stream=False, provider="", target_model="",
            )

        model = body.get("model", "unknown")
        stream = body.get("stream", False)
        verbose = logger.isEnabledFor(logging.DEBUG)

        try:
            adapter, provider_name, target_model, api_key = _route_vision(model, body)
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
        client = UpstreamClient(adapter, api_key, timeout=provider_timeout, stream_timeout=max(provider_timeout, 600))

        try:
            # 1. 协议转换: Responses → Chat
            cfg = get_config()
            chat_req = translate_request(body, adapter, target_model, alias=model)
            has_image_gen = chat_req.pop("_has_image_gen", False)

            # 从 model_mapping 读取 per-model thinking 配置
            model_entry = cfg.model_mapping.get(model)
            if isinstance(model_entry, list):
                model_item = next((e for e in model_entry if e.get("enabled")), model_entry[0] if model_entry else None)
            else:
                model_item = model_entry
            if model_item is not None:
                if not model_item.get("enable_thinking", True):
                    chat_req["_disable_thinking"] = True
                if "thinking_budget" in model_item:
                    chat_req["_thinking_budget"] = model_item["thinking_budget"]

            # image_gen 内置工具：不转发给 LLM，直接在 bridge 内处理
            if has_image_gen:
                return await _handle_responses_image_gen(body, cfg, model)

            logger.info("Chat 请求 → %s: model=%s, msgs=%d, tools=%d, stream=%s",
                target_model, chat_req.get("model"),
                len(chat_req.get("messages", [])),
                len(chat_req.get("tools", []) or []),
                chat_req.get("stream"))

            if verbose:
                _safe_log("Chat 请求详情", chat_req)

            if stream:
                # 2. 流式处理
                return StreamingResponse(
                    _handle_stream(client, adapter, chat_req, model, verbose, start_time, provider_name, target_model),
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
                responses_resp = translate_response(chat_resp, adapter, model)

                # 统计 token
                tokens = chat_resp.get("usage", {}).get("total_tokens", 0)

                if verbose:
                    _safe_log("Responses 响应", responses_resp)

                _record_request(start_time, model, "responses", 200, False, "", tokens, provider=provider_name, target_model=target_model)
                return JSONResponse(content=responses_resp)

        except Exception as exc:
            status_code = 500
            error_msg = str(exc)
            logger.exception("请求处理异常")
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
            body = await request.json()
        except Exception:
            return JSONResponse(
                content=build_error_response("无效的 JSON 请求体"),
                status_code=400,
            )

        model = body.get("model", "unknown")
        stream = body.get("stream", False)

        try:
            adapter, provider_name, target_model, api_key = _get_adapter_for_model(model)
        except ValueError as exc:
            _record_request(start_time, model, "chat", 400, stream, str(exc), provider="", target_model="")
            return JSONResponse(
                content=build_error_response(str(exc)),
                status_code=400,
            )

        body["model"] = target_model
        body = adapter.preprocess_chat_request(body)

        provider_timeout = get_config().get_provider(provider_name).get("timeout", 120) if provider_name else 120
        client = UpstreamClient(adapter, api_key, timeout=provider_timeout, stream_timeout=max(provider_timeout, 600))
        try:
            if stream:
                async def _sse_gen():
                    async for chunk in client.chat_completion_stream(body):
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    _sse_gen(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
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
            body = await request.json()
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
            adapter, _, _, api_key = _resolve_adapter(provider_name, gen_target)
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
        headers = adapter.get_headers(api_key)

        logger.info("生图请求 → %s/%s: prompt=%.80s..., size=%s",
            provider_name, gen_target,
            body.get("prompt", "")[:80],
            body.get("size", "1024x1024"))

        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
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

    return app


# ── 流式处理 ────────────────────────────────────────────────────────

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

    支持自动重试一次：上游连接断开时重建连接和 StreamTranslator，
    避免 translator 状态污染导致 Codex 收到混乱的 SSE 事件流。
    """
    import httpx

    RETRYABLE = (
        httpx.RemoteProtocolError, httpx.ReadTimeout,
        httpx.ConnectTimeout, httpx.ConnectError,
        ConnectionResetError, ConnectionAbortedError,
    )

    stream_error = ""
    for attempt in range(2):
        translator = StreamTranslator(model=model)
        chat_stream = client.chat_completion_stream(chat_req)

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(chat_stream), timeout=10.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                except StopAsyncIteration:
                    break

                chunk = adapter.stream_event_transform(chunk)

                if verbose:
                    _safe_log("Chat chunk", chunk)

                for event_line in translator.translate_chunk(chunk):
                    yield event_line

            # 流正常结束，发送 response.completed
            for event_line in translator._finish():
                yield event_line
            stream_error = ""
            break  # 成功，退出重试循环

        except RETRYABLE as exc:
            stream_error = str(exc)
            if attempt == 0:
                logger.warning("流式连接断开，1秒后重试: %s", exc)
                await asyncio.sleep(1)
                if client._stream_client:
                    await client._stream_client.aclose()
                    client._stream_client = None
                continue
            else:
                logger.exception("流式处理异常（重试失败）")

        except Exception as exc:
            stream_error = str(exc)
            logger.exception("流式处理异常")
            break

    # 流异常结束时发送 error 事件，让 Codex 知道请求已终止
    if stream_error:
        try:
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
    else:
        _record_request(start_time, model, "responses", 200, True, "", provider=provider, target_model=target_model)

    await client.close()


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
