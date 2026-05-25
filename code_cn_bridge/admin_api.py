"""管理 API —— 供桌面 UI 调用的配置管理端点"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from .config import get_config
from .adapters import get_registry
from .stats import get_stats, RequestLog
from .client import UpstreamClient

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
        "version": "0.3.6",
        "stats": stats.get_summary(),
    }


# ═══════════════════════════════════════════════════════════════════
# 模型 CRUD
# ═══════════════════════════════════════════════════════════════════

@router.get("/models")
async def list_models():
    """获取所有模型配置"""
    cfg = get_config()
    reg = get_registry()
    providers = cfg.providers
    mapping = cfg.model_mapping

    models = []
    for alias, entry in mapping.items():
        target = entry.get("target", alias)
        provider_name = entry.get("provider", "") or _find_provider_for_target(target, providers)
        provider = providers.get(provider_name, {})
        models.append({
            "alias": alias,
            "target_model": target,
            "provider": provider_name or "",
            "adapter": provider.get("adapter", ""),
            "base_url": provider.get("base_url", ""),
            "api_key_env": provider.get("api_key_env", ""),
            "api_key_set": bool(provider.get("api_key", "")),
            "enabled": entry.get("enabled", True) if isinstance(entry, dict) else provider.get("enabled", True),
            "is_multimodal": entry.get("is_multimodal", False),
            "vision_alias": entry.get("vision_alias") or "",
            "is_image_gen": entry.get("is_image_gen", False),
            "image_gen_alias": entry.get("image_gen_alias") or "",
            "is_video_gen": entry.get("is_video_gen", False),
            "video_gen_alias": entry.get("video_gen_alias") or "",
            "available_adapters": reg.list(),
        })
    return {"models": models}


@router.post("/models")
async def add_model(data: dict):
    """添加模型映射"""
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
        # 已有 provider：仅当前端明确传了非空值时才更新（防止空字符串覆盖已有配置）
        if data.get("adapter"):
            p["adapter"] = data["adapter"]
        if data.get("base_url"):
            p["base_url"] = data["base_url"]
        if data.get("api_key_env"):
            p["api_key_env"] = data["api_key_env"]

    # 更新 api_key（仅当提供了新 key）
    if data.get("api_key"):
        providers[provider_name]["api_key"] = data["api_key"]

    # 已有 provider 不覆盖 enabled，新 provider 使用传入值
    if is_new_provider and "enabled" in data:
        providers[provider_name]["enabled"] = data["enabled"]

    # 存储 model_mapping（新格式）
    mapping = cfg._data.setdefault("model_mapping", {})
    mapping[alias] = {
        "target": target,
        "provider": provider_name,
        "enabled": data.get("enabled", True),
        "is_multimodal": data.get("is_multimodal", False),
        "vision_alias": data.get("vision_alias") or None,
        "is_image_gen": data.get("is_image_gen", False),
        "image_gen_alias": data.get("image_gen_alias") or None,
        "is_video_gen": data.get("is_video_gen", False),
        "video_gen_alias": data.get("video_gen_alias") or None,
    }
    cfg.save()
    return {"status": "ok", "alias": alias}


@router.put("/models/{alias}")
async def update_model(alias: str, data: dict):
    """更新模型配置"""
    cfg = get_config()
    mapping = cfg._data.get("model_mapping", {})

    if alias not in mapping:
        return {"error": f"模型别名 '{alias}' 不存在"}, 404

    old_entry = mapping[alias]
    old_target = old_entry.get("target", old_entry) if isinstance(old_entry, dict) else old_entry

    target = data.get("target_model", old_target)
    providers = cfg._data.setdefault("providers", {})
    provider_name = data.get("provider", old_entry.get("provider", "") if isinstance(old_entry, dict) else "")

    # 回退：如果 provider_name 为空，从 target 反查 provider
    if not provider_name or provider_name not in providers:
        found = _find_provider_for_target(old_target, providers)
        if found:
            provider_name = found

    # 更新 model_mapping 条目
    old_dict = old_entry if isinstance(old_entry, dict) else {}
    mapping[alias] = {
        "target": target,
        "provider": provider_name,
        "enabled": data.get("enabled", old_dict.get("enabled", True)),
        "is_multimodal": data.get("is_multimodal", old_dict.get("is_multimodal", False)),
        "vision_alias": data.get("vision_alias") if "vision_alias" in data else old_dict.get("vision_alias"),
        "is_image_gen": data.get("is_image_gen", old_dict.get("is_image_gen", False)),
        "image_gen_alias": data.get("image_gen_alias") if "image_gen_alias" in data else old_dict.get("image_gen_alias"),
        "is_video_gen": data.get("is_video_gen", old_dict.get("is_video_gen", False)),
        "video_gen_alias": data.get("video_gen_alias") if "video_gen_alias" in data else old_dict.get("video_gen_alias"),
    }

    # 更新 provider
    if provider_name and provider_name in providers:
        p = providers[provider_name]
        if "base_url" in data:
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
    return {"status": "ok", "alias": alias}


@router.delete("/models/{alias}")
async def delete_model(alias: str):
    """删除模型映射"""
    cfg = get_config()
    mapping = cfg._data.get("model_mapping", {})

    if alias not in mapping:
        return {"error": f"模型别名 '{alias}' 不存在"}, 404

    del cfg._data["model_mapping"][alias]
    cfg.save()
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
# 连接测试
# ═══════════════════════════════════════════════════════════════════

@router.post("/models/{alias}/test")
async def test_connection(alias: str, data: dict | None = None):
    """测试模型连接"""
    cfg = get_config()
    reg = get_registry()
    mapping = cfg._data.get("model_mapping", {})

    entry = mapping.get(alias, alias)
    if isinstance(entry, dict):
        target = entry.get("target", alias)
        provider_name = entry.get("provider", "") or _find_provider_for_target(target, cfg.providers)
    else:
        target = entry
        provider_name = _find_provider_for_target(target, cfg.providers)

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
