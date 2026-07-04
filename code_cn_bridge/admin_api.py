"""管理 API —— 供桌面 UI 调用的配置管理端点"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .config import get_config
from .adapters import get_registry
from .stats import get_stats, RequestLog
from .client import UpstreamClient
from .catalog import get_codex_mode, switch_codex_mode

logger = logging.getLogger("code-cn-bridge")

router = APIRouter(prefix="/admin/api")


# ═══════════════════════════════════════════════════════════════════
# 状态
# ═══════════════════════════════════════════════════════════════════

@router.get("/status")
async def get_status():
    """代理运行状态"""
    cfg = get_config()
    stats = get_stats()
    return {
        "running": True,
        "host": cfg.server_host,
        "port": cfg.server_port,
        "version": "0.5.0",
        "stats": stats.get_summary(),
    }


# ═══════════════════════════════════════════════════════════════════
# 模型 CRUD
# ═══════════════════════════════════════════════════════════════════

def _build_model_entry(alias: str, entry: dict, providers: dict, available_adapters: list[str],
                        index: int | None = None, active: bool = False) -> dict:
    """从映射条目构建前端模型对象"""
    target = entry.get("target", alias)
    provider_name = entry.get("provider", "") or _find_provider_for_target(target, providers)
    provider = providers.get(provider_name, {})
    model = {
        "alias": alias,
        "target_model": target,
        "provider": provider_name or "",
        "adapter": provider.get("adapter", ""),
        "base_url": provider.get("base_url", ""),
        "api_key_env": provider.get("api_key_env", ""),
        "api_key_set": bool(provider.get("api_key", "")),
        "enabled": entry.get("enabled", True),
        "enable_thinking": entry.get("enable_thinking", True),
        "thinking_budget": entry.get("thinking_budget", 4096),
        "is_multimodal": entry.get("is_multimodal", False),
        "vision_alias": entry.get("vision_alias") or "",
        "is_image_gen": entry.get("is_image_gen", False),
        "image_gen_alias": entry.get("image_gen_alias") or "",
        "is_video_gen": entry.get("is_video_gen", False),
        "video_gen_alias": entry.get("video_gen_alias") or "",
        "available_adapters": available_adapters,
    }
    if index is not None:
        model["_index"] = index
    if active:
        model["_active"] = True
    return model


@router.get("/models")
async def list_models(response: Response):
    """获取所有模型配置（多模型列表条目已展开）"""
    cfg = get_config()
    reg = get_registry()
    providers = cfg.providers
    mapping = cfg.model_mapping
    adapters = reg.list()

    models = []
    for alias, entry in mapping.items():
        if isinstance(entry, list):
            for i, item in enumerate(entry):
                models.append(_build_model_entry(
                    alias, item, providers, adapters,
                    index=i, active=item.get("enabled", False)))
        elif isinstance(entry, dict):
            models.append(_build_model_entry(alias, entry, providers, adapters))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return {"models": models}


@router.post("/models")
async def add_model(data: dict):
    """添加模型映射（同名 alias 会追加为多模型列表）"""
    cfg = get_config()
    alias = data.get("alias", "").strip()
    target = data.get("target_model", "").strip()

    if not alias or not target:
        return {"error": "alias 和 target_model 为必填项"}, 400

    # 更新 provider 信息
    provider_name = data.get("provider", target)
    providers = cfg._data.setdefault("providers", {})

    is_new_provider = provider_name not in providers
    if is_new_provider:
        providers[provider_name] = {
            "adapter": data.get("adapter", provider_name),
            "base_url": data.get("base_url", ""),
            "api_key_env": data.get("api_key_env", ""),
            "enabled": data.get("enabled", True),
        }
    else:
        p = providers[provider_name]
        if data.get("adapter"):
            p["adapter"] = data["adapter"]
        if data.get("base_url"):
            p["base_url"] = data["base_url"]
        if data.get("api_key_env"):
            p["api_key_env"] = data["api_key_env"]

    if data.get("api_key"):
        providers[provider_name]["api_key"] = data["api_key"]
    if is_new_provider and "enabled" in data:
        providers[provider_name]["enabled"] = data["enabled"]

    # 构建新条目
    new_entry = {
        "target": target,
        "provider": provider_name,
        "enabled": data.get("enabled", True),
        "enable_thinking": data.get("enable_thinking", True),
        "thinking_budget": data.get("thinking_budget", 4096),
        "is_multimodal": data.get("is_multimodal", False),
        "vision_alias": data.get("vision_alias") or None,
        "is_image_gen": data.get("is_image_gen", False),
        "image_gen_alias": data.get("image_gen_alias") or None,
        "is_video_gen": data.get("is_video_gen", False),
        "video_gen_alias": data.get("video_gen_alias") or None,
    }

    mapping = cfg._data.setdefault("model_mapping", {})
    existing = mapping.get(alias)

    if existing is None:
        # 全新别名
        mapping[alias] = new_entry
    elif isinstance(existing, list):
        # 已是多模型列表，追加
        if new_entry["enabled"]:
            for e in existing:
                e["enabled"] = False
        existing.append(new_entry)
    elif isinstance(existing, dict):
        # 单模型 → 转为多模型列表
        existing["enabled"] = existing.get("enabled", True)
        if new_entry["enabled"]:
            existing["enabled"] = False
        mapping[alias] = [existing, new_entry]

    cfg.save()
    return {"status": "ok", "alias": alias}


@router.put("/models/{alias}")
async def update_model(alias: str, data: dict, _index: int | None = None):
    """更新模型配置（支持多模型列表的 _index 查询参数）"""
    cfg = get_config()
    mapping = cfg._data.get("model_mapping", {})

    if alias not in mapping:
        return {"error": f"模型别名 '{alias}' 不存在"}, 404

    raw_entry = mapping[alias]
    logger.info("PUT /models/%s _index=%s type=%s data_keys=%s",
        alias, _index, type(raw_entry).__name__, list(data.keys()))

    # 定位要更新的条目
    if isinstance(raw_entry, list):
        if _index is None or _index < 0 or _index >= len(raw_entry):
            return {"error": f"多模型列表需要有效的 _index (0~{len(raw_entry)-1})"}, 400
        old_entry = raw_entry[_index]
    else:
        if _index is not None and _index > 0:
            return {"error": "单模型不支持 _index"}, 400
        old_entry = raw_entry

    old_target = old_entry.get("target", alias) if isinstance(old_entry, dict) else old_entry
    target = data.get("target_model", old_target)
    providers = cfg._data.setdefault("providers", {})
    provider_name = data.get("provider", old_entry.get("provider", "") if isinstance(old_entry, dict) else "")

    if not provider_name or provider_name not in providers:
        found = _find_provider_for_target(old_target, providers)
        if found:
            provider_name = found

    old_dict = old_entry if isinstance(old_entry, dict) else {}
    new_enabled = data.get("enabled", old_dict.get("enabled", True))
    updated_entry = {
        "target": target,
        "provider": provider_name,
        "enabled": new_enabled,
        "enable_thinking": data.get("enable_thinking", old_dict.get("enable_thinking", True)),
        "thinking_budget": data.get("thinking_budget", old_dict.get("thinking_budget", 4096)),
        "is_multimodal": data.get("is_multimodal", old_dict.get("is_multimodal", False)),
        "vision_alias": data.get("vision_alias") if "vision_alias" in data else old_dict.get("vision_alias"),
        "is_image_gen": data.get("is_image_gen", old_dict.get("is_image_gen", False)),
        "image_gen_alias": data.get("image_gen_alias") if "image_gen_alias" in data else old_dict.get("image_gen_alias"),
        "is_video_gen": data.get("is_video_gen", old_dict.get("is_video_gen", False)),
        "video_gen_alias": data.get("video_gen_alias") if "video_gen_alias" in data else old_dict.get("video_gen_alias"),
    }

    logger.info("PUT /models/%s old=(target=%s provider=%s enabled=%s) → new=(target=%s provider=%s enabled=%s)",
        alias, old_target, old_dict.get("provider",""), old_dict.get("enabled",""),
        target, provider_name, new_enabled)

    # 更新：如果本条目被启用，禁用同 alias 其他条目
    if isinstance(raw_entry, list):
        if updated_entry["enabled"]:
            for i, e in enumerate(raw_entry):
                if i != _index:
                    e["enabled"] = False
        else:
            other_enabled = any(e.get("enabled") for i, e in enumerate(raw_entry) if i != _index)
            if not other_enabled and len(raw_entry) > 1:
                for i, e in enumerate(raw_entry):
                    if i != _index:
                        e["enabled"] = True
                        break

    # 处理别名重命名
    new_alias = data.get("alias", "").strip()
    effective_alias = alias
    if new_alias and new_alias != alias:
        # 确定要移动的条目
        if isinstance(raw_entry, list) and _index is not None:
            moving_entry = updated_entry  # 单个条目移出
            remaining = [e for i, e in enumerate(raw_entry) if i != _index]
        else:
            moving_entry = None  # 移动整个 raw_entry
            remaining = None

        if new_alias in mapping:
            # 目标已存在 → 合并
            if moving_entry:
                moving_entry["enabled"] = False
            existing = mapping[new_alias]
            if isinstance(existing, list):
                if moving_entry:
                    existing.append(moving_entry)
                else:
                    existing.extend(raw_entry)
            else:
                if moving_entry:
                    mapping[new_alias] = [existing, moving_entry]
                else:
                    mapping[new_alias] = [existing] + (raw_entry if isinstance(raw_entry, list) else [raw_entry])
        else:
            if moving_entry:
                mapping[new_alias] = moving_entry
            elif isinstance(raw_entry, list):
                mapping[new_alias] = raw_entry
            else:
                mapping[new_alias] = updated_entry

        # 从旧键名移除
        if moving_entry:
            if len(remaining) == 1:
                mapping[alias] = remaining[0]
            elif len(remaining) == 0:
                del mapping[alias]
            else:
                mapping[alias] = remaining
        else:
            del mapping[alias]
        effective_alias = new_alias
    else:
        if isinstance(raw_entry, list):
            raw_entry[_index] = updated_entry
        else:
            mapping[alias] = updated_entry

    # 更新 provider
    if provider_name and provider_name in providers:
        p = providers[provider_name]
        if "adapter" in data:
            p["adapter"] = data["adapter"]
        if "base_url" in data:
            logger.info("PUT /models/%s updating base_url: %s → %s", alias, p.get("base_url"), data["base_url"])
            p["base_url"] = data["base_url"]
        if "api_key" in data and data["api_key"]:
            p["api_key"] = data["api_key"]
        if "api_key_env" in data:
            p["api_key_env"] = data["api_key_env"]
        if "enabled" in data:
            p["enabled"] = data["enabled"]

        advanced = data.get("advanced", {})
        if advanced:
            p["timeout"] = advanced.get("timeout", p.get("timeout", 120))
            p["max_retries"] = advanced.get("max_retries", p.get("max_retries", 0))
            p["tool_calls_enabled"] = advanced.get("tool_calls_enabled", p.get("tool_calls_enabled", True))
            p["extra_headers"] = advanced.get("extra_headers", p.get("extra_headers", {}))

    cfg.save()
    logger.info("PUT /models/%s saved successfully (effective=%s)", alias, effective_alias)
    return {"status": "ok", "alias": effective_alias}


@router.delete("/models/{alias}")
async def delete_model(alias: str, _index: int | None = None):
    """删除模型映射（支持多模型列表的 _index 查询参数）"""
    cfg = get_config()
    mapping = cfg._data.get("model_mapping", {})

    if alias not in mapping:
        return {"error": f"模型别名 '{alias}' 不存在"}, 404

    entry = mapping[alias]
    if isinstance(entry, list):
        if _index is None:
            return {"error": f"多模型列表需要 _index 参数 (0~{len(entry)-1})"}, 400
        if _index < 0 or _index >= len(entry):
            return {"error": f"_index 超出范围 (0~{len(entry)-1})"}, 400
        removed = entry.pop(_index)
        # 如果删除的是启用的，激活第一个剩余条目
        if removed.get("enabled") and entry:
            entry[0]["enabled"] = True
        # 如果只剩一个，降级为单模型
        if len(entry) == 1:
            mapping[alias] = entry[0]
        elif len(entry) == 0:
            del mapping[alias]
    else:
        if _index is not None:
            return {"error": "单模型不支持 _index"}, 400
        del cfg._data["model_mapping"][alias]

    cfg.save()
    return {"status": "ok"}


@router.post("/models/{alias}/activate/{index}")
async def activate_model(alias: str, index: int):
    """激活多模型列表中指定索引的条目（切换当前使用的后端）"""
    cfg = get_config()
    mapping = cfg._data.get("model_mapping", {})

    if alias not in mapping:
        return {"error": f"模型别名 '{alias}' 不存在"}, 404

    entry = mapping[alias]
    if not isinstance(entry, list):
        return {"error": "单模型无需激活/切换"}, 400
    if index < 0 or index >= len(entry):
        return {"error": f"索引超出范围 (0~{len(entry)-1})"}, 400

    # 激活指定条目，禁用其他
    for i, e in enumerate(entry):
        e["enabled"] = (i == index)

    cfg.save()
    return {"status": "ok", "alias": alias, "active_index": index}


# ═══════════════════════════════════════════════════════════════════
# 连接测试
# ═══════════════════════════════════════════════════════════════════

def _resolve_test_entry(cfg, alias: str, index: int | None = None) -> tuple[str, str, str]:
    """解析测试连接用的 (provider_name, target, adapter_name)"""
    providers = cfg.providers
    mapping = cfg._data.get("model_mapping", {})

    entry = mapping.get(alias, alias)
    if isinstance(entry, list):
        if index is not None and 0 <= index < len(entry):
            item = entry[index]
        else:
            # 取第一个 enabled 的
            item = next((e for e in entry if e.get("enabled")), entry[0])
        target = item.get("target", alias)
        provider_name = item.get("provider", "") or _find_provider_for_target(target, providers)
    elif isinstance(entry, dict):
        target = entry.get("target", alias)
        provider_name = entry.get("provider", "") or _find_provider_for_target(target, providers)
    else:
        target = entry
        provider_name = _find_provider_for_target(target, providers)
    return provider_name, target, provider_name


@router.post("/models/{alias}/test")
async def test_connection(alias: str, data: dict | None = None):
    """测试模型连接"""
    cfg = get_config()
    reg = get_registry()
    _index = data.get("_index") if data else None

    provider_name, target, _ = _resolve_test_entry(cfg, alias, _index)

    if not provider_name:
        return {"status": "error", "message": f"未找到模型 '{alias}' 的 provider 配置"}

    provider = cfg.providers[provider_name]
    adapter_name = provider.get("adapter", provider_name)
    adapter = reg.get(adapter_name)
    if not adapter:
        return {"status": "error", "message": f"未找到适配器 '{adapter_name}'"}

    api_key = data.get("api_key") if data else None
    if not api_key:
        api_key = provider.get("api_key", "")
    if not api_key:
        return {"status": "error", "message": "API Key 未设置"}

    # 临时覆盖 base_url
    if data and data.get("base_url"):
        adapter.base_url = data["base_url"]
    elif provider.get("base_url"):
        adapter.base_url = provider["base_url"]

    # 构建两种测试请求
    headers = adapter.get_headers(api_key)

    # 先尝试 chat 端点
    chat_url = adapter.build_chat_url()
    chat_body = adapter.preprocess_chat_request({
        "model": target,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 5,
        "stream": False,
    })

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as client:
            resp = await client.post(chat_url, json=chat_body, headers=headers)
            elapsed = (time.time() - start) * 1000

            if resp.status_code == 200:
                return {
                    "status": "ok",
                    "elapsed_ms": round(elapsed, 1),
                    "message": f"连接成功 ({resp.status_code})",
                }

            # 如果 chat 失败且提示不支持该 API，回退到生图端点
            resp_text = resp.text.lower()
            if resp.status_code in (400, 404) and ("not support" in resp_text or "does not support" in resp_text or "not valid" in resp_text):
                img_url = adapter.build_image_gen_url()
                img_body = adapter.preprocess_image_gen_request({
                    "model": target,
                    "prompt": "test",
                    "n": 1,
                    "size": "256x256",
                })
                img_start = time.time()
                img_resp = await client.post(img_url, json=img_body, headers=headers)
                img_elapsed = (time.time() - img_start) * 1000

                if img_resp.status_code == 200:
                    return {
                        "status": "ok",
                        "elapsed_ms": round(img_elapsed, 1),
                        "message": f"生图连接成功 ({img_resp.status_code})",
                    }
                return {
                    "status": "error",
                    "elapsed_ms": round(img_elapsed, 1),
                    "message": f"HTTP {img_resp.status_code}: {img_resp.text[:200]}",
                }

            return {
                "status": "error",
                "elapsed_ms": round(elapsed, 1),
                "message": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
    except httpx.TimeoutException:
        return {"status": "error", "message": "连接超时（15秒）"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ═══════════════════════════════════════════════════════════════════
# 全局设置
# ═══════════════════════════════════════════════════════════════════

@router.get("/settings")
async def get_settings():
    """获取全局设置"""
    cfg = get_config()
    return {
        "server": {
            "host": cfg.server_host,
            "port": cfg.server_port,
            "log_level": cfg._data.get("server", {}).get("log_level", "info"),
            "auto_start": cfg._data.get("server", {}).get("auto_start", False),
            "close_to_tray": cfg._data.get("server", {}).get("close_to_tray", True),
            "audit_log_path": cfg._data.get("server", {}).get("audit_log_path", ""),
            "update_mirror": cfg._data.get("server", {}).get("update_mirror", ""),
        },
        "config_path": str(cfg._config_path) if cfg._config_path else "",
    }


@router.put("/settings")
async def update_settings(data: dict):
    """更新全局设置"""
    cfg = get_config()
    server_cfg = cfg._data.setdefault("server", {})

    if "host" in data:
        server_cfg["host"] = data["host"]
    if "port" in data:
        server_cfg["port"] = int(data["port"])
    if "log_level" in data:
        server_cfg["log_level"] = data["log_level"]
    if "auto_start" in data:
        server_cfg["auto_start"] = data["auto_start"]
    if "close_to_tray" in data:
        server_cfg["close_to_tray"] = data["close_to_tray"]
    if "audit_log_path" in data:
        server_cfg["audit_log_path"] = data["audit_log_path"]
    if "update_mirror" in data:
        server_cfg["update_mirror"] = data["update_mirror"]

    cfg.save()
    return {"status": "ok", "message": "设置已保存，部分设置需重启后生效"}


# ═══════════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════════

@router.get("/logs")
async def get_logs(limit: int = 100):
    """获取最近请求日志"""
    stats = get_stats()
    return {"logs": stats.get_recent_logs(limit)}


@router.post("/logs/clear")
async def clear_logs():
    """清空日志"""
    get_stats().clear_logs()
    return {"status": "ok"}


@router.websocket("/logs/stream")
async def logs_stream(websocket: WebSocket):
    """WebSocket 实时日志流"""
    await websocket.accept()

    queue: asyncio.Queue = asyncio.Queue()

    def on_log(entry: dict):
        try:
            queue.put_nowait(entry)
        except Exception:
            pass

    stats = get_stats()
    stats.add_listener(on_log)

    try:
        while True:
            entry = await queue.get()
            await websocket.send_json(entry)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stats.remove_listener(on_log)


# ═══════════════════════════════════════════════════════════════════
# 详细日志（完整请求/响应捕获）
# ═══════════════════════════════════════════════════════════════════

@router.get("/detailed-logs/status")
async def get_detailed_logs_status():
    """获取详细日志开关状态"""
    stats = get_stats()
    return {
        "enabled": stats.detailed_enabled,
        "count": len(stats.get_detailed_logs()),
    }


@router.post("/detailed-logs/toggle")
async def toggle_detailed_logs(data: dict | None = None):
    """开关详细日志"""
    stats = get_stats()
    enabled = (data or {}).get("enabled", not stats.detailed_enabled)
    stats.set_detailed_enabled(enabled)
    return {"enabled": enabled, "message": "详细日志已开启" if enabled else "详细日志已关闭"}


@router.get("/detailed-logs")
async def get_detailed_logs():
    """获取详细日志列表（最近 100 条）"""
    stats = get_stats()
    return {"logs": stats.get_detailed_logs()}


@router.post("/detailed-logs/clear")
async def clear_detailed_logs():
    """清空详细日志"""
    get_stats().clear_detailed_logs()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
# 配置导入导出
# ═══════════════════════════════════════════════════════════════════

@router.get("/config/export")
async def export_config():
    """导出完整配置（YAML 格式）"""
    import yaml
    cfg = get_config()
    # 深拷贝并移除敏感信息
    data = json.loads(json.dumps(cfg._data))
    for p in data.get("providers", {}).values():
        p.pop("api_key", None)
    return {
        "yaml": yaml.dump(data, allow_unicode=True, default_flow_style=False),
        "config_path": str(cfg._config_path) if cfg._config_path else "",
    }


@router.post("/config/import")
async def import_config(data: dict):
    """导入配置"""
    import yaml
    cfg = get_config()
    yaml_str = data.get("yaml", "")
    if not yaml_str:
        return {"error": "缺少 yaml 字段"}, 400
    try:
        new_data = yaml.safe_load(yaml_str)
        # 深度合并
        cfg._data = _deep_merge(cfg._data, new_data)
        cfg.save()
        return {"status": "ok"}
    except Exception as exc:
        return {"error": str(exc)}, 400


@router.post("/shutdown")
async def shutdown():
    """安全关闭代理"""
    import os
    import signal

    def _do_shutdown():
        # 延迟一瞬让响应返回
        import time
        time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    import threading
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"status": "ok", "message": "正在关闭..."}


# ═══════════════════════════════════════════════════════════════════
# 熔断器管理
# ═══════════════════════════════════════════════════════════════════

@router.get("/circuit-breakers")
async def list_circuit_breakers():
    """返回所有 provider 的熔断器状态和健康评分"""
    from .circuit_breaker import get_circuit_breaker_registry
    try:
        registry = get_circuit_breaker_registry()
        breakers = []
        for name, breaker in registry.get_all().items():
            ws = breaker.window_stats
            breakers.append({
                "name": name,
                "state": breaker.state.name,
                "health_score": breaker.health_score,
                "window_stats": {
                    "total": ws.get("total_requests", 0),
                    "failures": ws.get("failures", 0),
                    "error_rate": ws.get("error_rate", 0.0),
                },
            })
        return {"breakers": breakers}
    except Exception as exc:
        logger.error("获取熔断器状态失败: %s", exc, exc_info=True)
        return {"breakers": [], "error": str(exc)}


@router.post("/circuit-breakers/{provider}/reset")
async def reset_circuit_breaker(provider: str):
    """手动重置指定 provider 的熔断器"""
    from .circuit_breaker import get_circuit_breaker_registry
    try:
        registry = get_circuit_breaker_registry()
        registry.reset(provider)
        return {"success": True, "provider": provider}
    except Exception as exc:
        logger.error("重置熔断器 %s 失败: %s", provider, exc, exc_info=True)
        return {"success": False, "provider": provider, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════
# ResponseCache 管理
# ═══════════════════════════════════════════════════════════════════

@router.get("/cache/responses")
async def get_cache_stats():
    """返回 ResponseCache 统计"""
    from .protocol import get_response_cache
    try:
        cache = get_response_cache()
        if hasattr(cache, "stats"):
            stats = cache.stats()
            return {
                "count": stats.get("count", 0),
                "disk_count": stats.get("disk_count", 0),
            }
        logger.warning("ResponseCache.stats() 方法不存在")
        return {"count": 0, "disk_count": 0}
    except Exception as exc:
        logger.error("获取缓存统计失败: %s", exc, exc_info=True)
        return {"count": 0, "disk_count": 0}


@router.delete("/cache/responses")
async def clear_cache():
    """清理全部缓存"""
    from .protocol import get_response_cache
    try:
        cache = get_response_cache()
        if hasattr(cache, "clear_all"):
            cache.clear_all()
            return {"success": True, "cleared": True}
        logger.warning("ResponseCache.clear_all() 方法不存在")
        return {"success": False, "cleared": False, "error": "clear_all 方法不存在"}
    except Exception as exc:
        logger.error("清理缓存失败: %s", exc, exc_info=True)
        return {"success": False, "cleared": False, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════
# Codex 配置一键导出
# ═══════════════════════════════════════════════════════════════════

def _collect_enabled_models(cfg) -> list[tuple[str, str, str]]:
    """收集已启用的模型: [(alias, target, provider), ...]"""
    enabled = []
    mapping = cfg.model_mapping
    for alias, entry in mapping.items():
        if isinstance(entry, list):
            for item in entry:
                if item.get("enabled", True):
                    target = item.get("target", alias)
                    provider = item.get("provider", "")
                    enabled.append((alias, target, provider))
                    break
        elif isinstance(entry, dict):
            if entry.get("enabled", True):
                target = entry.get("target", alias)
                provider = entry.get("provider", "")
                enabled.append((alias, target, provider))
    return enabled


@router.get("/codex-config")
async def export_codex_config():
    """生成可直接写入 ~/.codex/config.toml 的完整配置（解决登录白屏/Reconnecting 问题）"""
    try:
        cfg = get_config()
        host = cfg.server_host
        port = cfg.server_port
        endpoint = f"http://{host}:{port}/v1"

        enabled_models = _collect_enabled_models(cfg)

        # 选默认模型：优先代码模型，其次第一个启用的
        code_aliases = {"deepseek-v4", "gpt-5-code", "kimi-k2-7-code", "qwen3-coder-plus"}
        default_alias = ""
        default_target = ""
        for alias, target, _ in enabled_models:
            if alias in code_aliases:
                default_alias = alias
                default_target = target
                break
        if not default_alias and enabled_models:
            default_alias = enabled_models[0][0]
            default_target = enabled_models[0][1]

        toml_lines = [
            "# ════════════════════════════════════════════════════════════════",
            "# Code CN Bridge - Codex 配置（自动生成）",
            "# 写入位置: ~/.codex/config.toml (Windows: %USERPROFILE%\\.codex\\config.toml)",
            "# 关键: requires_openai_auth=false 跳过登录, supports_websockets=false 解决 Reconnecting",
            "# ════════════════════════════════════════════════════════════════",
            "",
            "# 默认 provider 和模型（可被 codex -m <alias> 覆盖）",
            f'model_provider = "code-cn-bridge"',
            f'model = "{default_alias}"' if default_alias else '# model = "deepseek-v4"',
            "",
            "[model_providers.code-cn-bridge]",
            'name = "Code CN Bridge"',
            f'base_url = "{endpoint}"',
            'env_key = "OPENAI_API_KEY"',
            'wire_api = "responses"',
            'requires_openai_auth = false',
            'supports_websockets = false',
            "",
        ]

        # 模型信息表（让 Codex 知道每个模型的能力，与 /v1/models 端点一致）
        if enabled_models:
            toml_lines.append("# 启用的模型列表及能力声明（与 /v1/models 端点一致）")
            for alias, target, provider_name in enabled_models:
                # 读取模型条目判断能力
                entry = cfg.model_mapping.get(alias)
                if isinstance(entry, list):
                    item = next((e for e in entry if e.get("enabled")), entry[0] if entry else {})
                else:
                    item = entry or {}
                is_img = item.get("is_image_gen", False)
                is_vid = item.get("is_video_gen", False)
                is_mm = item.get("is_multimodal", False)
                is_thinking = item.get("enable_thinking", False)

                # 估算上下文窗口（与 /v1/models 一致）
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

                toml_lines.append(f'[model_providers.code-cn-bridge.model_info."{alias}"]')
                toml_lines.append(f'name = "{target}"')
                toml_lines.append(f'context_window = {ctx}')
                toml_lines.append(f'supports_tool_calls = {"true" if not is_img and not is_vid else "false"}')
                toml_lines.append(f'supports_streaming = true')
                toml_lines.append(f'supports_vision = {"true" if is_mm else "false"}')
                toml_lines.append(f'supports_image_gen = {"true" if is_img else "false"}')
                toml_lines.append(f'supports_video_gen = {"true" if is_vid else "false"}')
                toml_lines.append(f'supports_reasoning = {"true" if is_thinking else "false"}')
                toml_lines.append("")

        # 使用示例
        toml_lines.extend([
            "# ════════════════════════════════════════════════════════════════",
            "# 使用示例:",
            "#   codex                    # 用默认模型",
            "#   codex -m deepseek-v4     # 指定模型",
            "#   codex -m qwen3.7-max     # 通义千问",
            "#   codex -m glm-5.2         # 智谱 GLM",
            "#   codex -m kimi-k2-7-code  # Kimi 长上下文",
            "#",
            "# 内置工具（bridge 已支持）:",
            "#   web_search          # 联网搜索",
            "#   file_search         # 文件检索",
            "#   code_interpreter    # Python 代码执行",
            "#   image_gen           # 生图（需启用 agnes-image 等模型）",
            "#   video_gen           # 生视频（需启用 agnes-video 等模型）",
            "# ════════════════════════════════════════════════════════════════",
        ])

        toml_str = "\n".join(toml_lines)
        return {
            "config": toml_str,
            "endpoint": endpoint,
            "default_model": default_alias,
            "enabled_count": len(enabled_models),
            "enabled_models": [{"alias": a, "target": t, "provider": p} for a, t, p in enabled_models],
        }
    except Exception as exc:
        logger.error("生成 Codex 配置失败: %s", exc, exc_info=True)
        return {"error": str(exc)}


@router.get("/codex-auth")
async def export_codex_auth():
    """检查 ~/.codex/auth.json 官方登录态（CC Switch v3.16.0+ 保留机制）

    核心原则（参考 CC Switch v3.16.4 codex-official-auth-preservation-guide）：
    - **不生成占位 auth.json**：覆盖官方登录态会导致 Codex 桌面端门控隐藏
      自定义模型与插件功能（computer_use/web_search/apply_patch 等）
    - 第三方 API Key 只写进 config.toml 的 [model_providers.xxx] 段
    - auth.json 必须保留官方 ChatGPT/Codex 登录缓存
    """
    try:
        cfg = get_config()
        host = cfg.server_host
        port = cfg.server_port
        codex_home = Path.home() / ".codex"
        auth_path = codex_home / "auth.json"

        # 检查官方登录态是否存在且有效
        auth_status = "unknown"
        auth_exists = auth_path.exists()
        has_official_token = False
        auth_keys: list[str] = []

        if auth_exists:
            try:
                auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
                auth_keys = list(auth_data.keys()) if isinstance(auth_data, dict) else []
                # 官方登录态的特征字段：tokens / account_id / id_token 等
                has_official_token = any(
                    k in auth_data
                    for k in ("tokens", "account_id", "id_token", "access_token")
                ) if isinstance(auth_data, dict) else False
                # 检测是否被旧的占位 key 覆盖（项目历史遗留问题）
                if isinstance(auth_data, dict) and auth_data.get("OPENAI_API_KEY") == "sk-bridge-local":
                    auth_status = "corrupted"  # 被占位 key 覆盖，需要重新登录
                elif has_official_token:
                    auth_status = "valid"
                else:
                    auth_status = "incomplete"
            except Exception as e:
                auth_status = "parse_error"
                logger.warning("解析 auth.json 失败: %s", e)
        else:
            auth_status = "missing"

        return {
            "auth_path": str(auth_path),
            "config_path": str(codex_home / "config.toml"),
            "endpoint": f"http://{host}:{port}/v1",
            "auth_status": auth_status,
            "auth_exists": auth_exists,
            "has_official_token": has_official_token,
            "auth_keys": auth_keys,
            # 关键：不再返回 auth_json 内容，避免前端覆盖官方登录态
            "preserve_official_auth": True,
            "instructions": _build_auth_instructions(auth_status),
        }
    except Exception as exc:
        logger.error("检查 Codex auth 失败: %s", exc, exc_info=True)
        return {"error": str(exc)}


def _build_auth_instructions(auth_status: str) -> list[str]:
    """根据 auth.json 状态生成对应的操作指引

    参考 CC Switch v3.16.4 codex-desktop-custom-model-visibility-zh.md：
    - Codex 桌面端通过 auth.json 检测官方登录态来放行自定义模型和插件
    - 任何覆盖 auth.json 的行为都会触发门控回落
    """
    if auth_status == "valid":
        return [
            "✓ 已检测到官方 ChatGPT/Codex 登录态，请保持 ~/.codex/auth.json 不变",
            "✓ 第三方 API Key 已写入 config.toml 的 [model_providers.custom] 段",
            "✓ 模型请求会走 bridge 转换，但插件/手机远程等官方功能继续可用",
            "1. 确保 bridge 正在运行（桌面应用已启动）",
            "2. 打开 Codex 桌面端，自定义模型应可见且插件可用",
            "警告：切勿用任何工具覆盖 auth.json，否则插件功能会失效",
        ]
    if auth_status == "corrupted":
        return [
            "✗ 检测到 auth.json 被占位 key (sk-bridge-local) 覆盖，这正是插件失效的根因",
            "修复步骤：",
            "1. 完全退出 Codex 桌面端（任务栏托盘右键 exit）",
            "2. 删除 ~/.codex/auth.json",
            "3. 打开 Codex 桌面端，使用 ChatGPT 账号登录一次（建立官方登录态）",
            "4. 登录后完全退出 Codex（托盘 exit）",
            "5. 启动 bridge 桌面应用，重新生成 config.toml（不会动 auth.json）",
            "6. 打开 Codex 桌面端，自定义模型和插件都应可用",
        ]
    if auth_status == "missing":
        return [
            "✗ 未检测到 ~/.codex/auth.json，Codex 桌面端会门控隐藏自定义模型和插件",
            "修复步骤：",
            "1. 打开 Codex 桌面端或 CLI",
            "2. 使用 ChatGPT 账号登录（建立官方登录态到 ~/.codex/auth.json）",
            "3. 登录后完全退出 Codex（任务栏托盘右键 exit）",
            "4. 启动 bridge 桌面应用，配置第三方模型",
            "5. 打开 Codex 桌面端，自定义模型和插件都应可用",
            "警告：bridge 不会自动生成 auth.json，必须通过官方登录建立",
        ]
    # incomplete / parse_error / unknown
    return [
        f"! auth.json 状态异常：{auth_status}",
        "建议：完全退出 Codex，重新用 ChatGPT 账号登录一次以重建官方登录态",
        "bridge 不会修改 auth.json，第三方 API Key 只写入 config.toml",
    ]


# ═══════════════════════════════════════════════════════════════════
# Codex 配置模式切换（参考 CC Switch 的配置切换器）
# ═══════════════════════════════════════════════════════════════════

@router.get("/codex-mode")
async def get_codex_mode_endpoint():
    """获取当前 Codex 配置模式

    返回：
    - mode: "official"（官方 ChatGPT 账号）或 "bridge"（桥接器代理）
    - auth_status: auth.json 官方登录态状态
    """
    try:
        mode = get_codex_mode()
        # 顺便返回 auth 状态，让前端显示登录态是否可用
        auth_path = Path.home() / ".codex" / "auth.json"
        auth_status = "missing"
        if auth_path.exists():
            try:
                auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
                if isinstance(auth_data, dict):
                    if auth_data.get("OPENAI_API_KEY") == "sk-bridge-local":
                        auth_status = "corrupted"
                    elif any(k in auth_data for k in ("tokens", "account_id", "id_token", "access_token")):
                        auth_status = "valid"
                    else:
                        auth_status = "incomplete"
            except Exception:
                auth_status = "parse_error"

        return {
            "mode": mode,
            "auth_status": auth_status,
            "mode_description": _mode_description(mode),
        }
    except Exception as exc:
        logger.error("获取 Codex 模式失败: %s", exc, exc_info=True)
        return {"error": str(exc), "mode": "unknown"}


@router.post("/codex-mode")
async def switch_codex_mode_endpoint(request: Request):
    """切换 Codex 配置模式

    请求体：{"mode": "official" | "bridge"}

    - official: 使用官方 ChatGPT 账号，移除桥接器字段
    - bridge: 使用桥接器代理，添加 model_provider/base_url/catalog 等
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"error": "无效的 JSON 请求体"},
            status_code=400,
        )

    mode = body.get("mode", "").strip().lower()
    if mode not in ("official", "bridge"):
        return JSONResponse(
            content={"error": f"无效的 mode: {mode}（支持: official / bridge）"},
            status_code=400,
        )

    result = switch_codex_mode(mode)
    status_code = 200 if result.get("success") else 400
    return JSONResponse(content=result, status_code=status_code)


def _mode_description(mode: str) -> str:
    """模式中文描述"""
    if mode == "bridge":
        return "桥接器模式：使用国产模型（DeepSeek/Kimi/GLM 等），通过本地代理转换协议"
    if mode == "official":
        return "官方模式：使用 ChatGPT 账号额度，官方模型和插件全功能可用"
    return f"未知模式: {mode}"


# ═══════════════════════════════════════════════════════════════════
# Provider 预设 & Codex 安装状态
# ═══════════════════════════════════════════════════════════════════

# 内置 provider 预设模板：用户添加卡片时选预设即可自动填充 base_url/adapter/api_key_env
# 字段说明：
#   name:        provider 标识（写入 config.yaml 的 key）
#   label:       展示名
#   adapter:     适配器类型
#   base_url:    API 基础地址
#   api_key_env: 环境变量名
#   docs_url:    API Key 申请文档
#   models:      推荐模型列表（target_model）
_PROVIDER_PRESETS: list[dict] = [
    {
        "name": "deepseek",
        "label": "DeepSeek 深度求索",
        "adapter": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "docs_url": "https://platform.deepseek.com/api_keys",
        "models": ["deepseek-v4", "deepseek-v4-pro", "deepseek-reasoner"],
    },
    {
        "name": "zhipu",
        "label": "智谱 GLM",
        "adapter": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY",
        "docs_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "models": ["glm-5", "glm-5.1", "glm-5.2", "glm-4-plus"],
    },
    {
        "name": "qwen",
        "label": "通义千问 阿里云",
        "adapter": "qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "QWEN_API_KEY",
        "docs_url": "https://bailian.console.aliyun.com/?apiKey=1#/api-key",
        "models": ["qwen3-coder-plus", "qwen3.7-max", "qwen3.7-plus"],
    },
    {
        "name": "kimi",
        "label": "Kimi 月之暗面",
        "adapter": "kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "KIMI_API_KEY",
        "docs_url": "https://platform.moonshot.cn/console/api-keys",
        "models": ["kimi-k2-6", "kimi-k2-7-code"],
    },
    {
        "name": "doubao",
        "label": "豆包 字节跳动",
        "adapter": "doubao",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key_env": "ARK_API_KEY",
        "docs_url": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "models": ["doubao-pro-1-5", "doubao-seed-1-8", "doubao-seed-2-0"],
    },
    {
        "name": "ernie",
        "label": "文心一言 百度",
        "adapter": "ernie",
        "base_url": "https://qianfan.baidubce.com/v2",
        "api_key_env": "ERNIE_API_KEY",
        "docs_url": "https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
        "models": ["ernie-5.1", "ernie-speed-pro-128k"],
    },
    {
        "name": "hunyuan",
        "label": "混元 腾讯",
        "adapter": "hunyuan",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "api_key_env": "HUNYUAN_API_KEY",
        "docs_url": "https://console.cloud.tencent.com/hunyuan/api-key",
        "models": ["hunyuan-pro", "hunyuan-turbo"],
    },
    {
        "name": "minimax",
        "label": "MiniMax",
        "adapter": "minimax",
        "base_url": "https://api.minimaxi.com/v1",
        "api_key_env": "MINIMAX_API_KEY",
        "docs_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "models": ["MiniMax-M2.7", "MiniMax-M3"],
    },
    {
        "name": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "adapter": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "docs_url": "https://cloud.siliconflow.cn/account/ak",
        "models": ["deepseek-ai/DeepSeek-V4", "Qwen/Qwen3.7-Max"],
    },
    {
        "name": "spark",
        "label": "讯飞星火",
        "adapter": "spark",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "api_key_env": "SPARK_API_KEY",
        "docs_url": "https://console.xfyun.cn/services/bm4",
        "models": ["spark-max", "spark-pro"],
    },
    {
        "name": "agnes",
        "label": "Agnes 聚合",
        "adapter": "agnes",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY",
        "docs_url": "",
        "models": ["agnes-1.5-flash", "agnes-2.0-flash", "agnes-image-2.1-flash", "agnes-video-v2.0"],
    },
    {
        "name": "ollama",
        "label": "Ollama 本地",
        "adapter": "ollama",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "docs_url": "https://ollama.com/download",
        "models": ["qwen3:latest", "deepseek-v3:latest", "llama3:latest"],
    },
]


@router.get("/provider-presets")
async def get_provider_presets():
    """返回内置 provider 预设模板列表

    供前端"添加模型卡片"时选择预设，自动填充 base_url/adapter/api_key_env。
    """
    return {"presets": _PROVIDER_PRESETS}


@router.get("/codex-status")
async def get_codex_status():
    """返回 Codex 安装与登录状态的综合信息

    供前端 Models 页面顶部"官方 Codex 状态卡"使用：
    - codex_installed: ~/.codex 目录是否存在（Codex 桌面端/CLI 是否安装过）
    - config_exists:  config.toml 是否存在
    - auth_status:    auth.json 官方登录态状态
    - mode:           当前配置模式（official/bridge）
    - download_url:   Codex 官方下载链接
    """
    codex_home = Path.home() / ".codex"
    config_path = codex_home / "config.toml"
    auth_path = codex_home / "auth.json"

    codex_installed = codex_home.exists()
    config_exists = config_path.exists()

    auth_status = "missing"
    if auth_path.exists():
        try:
            auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
            if isinstance(auth_data, dict):
                if auth_data.get("OPENAI_API_KEY") == "sk-bridge-local":
                    auth_status = "corrupted"
                elif any(k in auth_data for k in ("tokens", "account_id", "id_token", "access_token")):
                    auth_status = "valid"
                else:
                    auth_status = "incomplete"
        except Exception:
            auth_status = "parse_error"

    try:
        mode = get_codex_mode()
    except Exception:
        mode = "official"

    return {
        "codex_installed": codex_installed,
        "config_exists": config_exists,
        "auth_status": auth_status,
        "mode": mode,
        "download_url": "https://chatgpt.com/codex",
        "auth_guide": (
            "打开 Codex 桌面端 → 使用 ChatGPT 账号登录（不要用 API Key 登录），"
            "登录成功后会自动写入 ~/.codex/auth.json"
        ),
    }


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════

def _find_provider_for_target(target: str, providers: dict) -> str | None:
    """根据 target 名查找对应的 provider"""
    for pname, pinfo in providers.items():
        if pinfo.get("adapter") == target or pname == target:
            return pname
    for pname in providers:
        if pname in target.lower():
            return pname
    return next(iter(providers), None) if providers else None


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# 兼容别名：server.py 使用 `from .admin_api import router as admin_router`，
# 同时支持 `from code_cn_bridge.admin_api import admin_router` 直接导入。
admin_router = router
