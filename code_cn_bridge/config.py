"""配置管理 —— YAML 配置文件加载、环境变量注入、热加载"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".code-cn-bridge.yaml",
    Path("config.yaml"),
]


def _load_dotenv(dotenv_path: Path) -> None:
    """简易 .env 解析器，无需 python-dotenv 依赖"""
    if not dotenv_path.is_file():
        return
    try:
        for line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class Config:
    """配置管理器"""

    def __init__(self, config_path: str | Path | None = None):
        self._config_path: Path | None = None
        self._data: dict[str, Any] = {}
        self._lock = threading.RLock()
        self.load(config_path)

    # ── 加载 ─────────────────────────────────────────────────────

    def load(self, config_path: str | Path | None = None) -> None:
        """加载配置文件并注入环境变量"""
        path = self._resolve_path(config_path)
        # 自动加载 .env 文件（优先级：config 目录 > 用户主目录）
        if path:
            _load_dotenv(path.parent / ".env")
        _load_dotenv(Path.home() / ".code-cn-bridge.env")
        if path and path.exists():
            with self._lock:
                self._config_path = path
                self._data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            with self._lock:
                # 即使文件不存在，也记录默认路径，确保后续 save() 能写入
                self._config_path = path or (Path.home() / ".code-cn-bridge.yaml")
                self._data = {}
        self._inject_env()

    def reload(self) -> None:
        """重新加载配置（热加载用）"""
        self.load(self._config_path)

    def save(self) -> None:
        """保存当前配置到文件（原子写入，防止中途崩溃导致配置文件损坏）"""
        with self._lock:
            if self._config_path:
                self._config_path.parent.mkdir(parents=True, exist_ok=True)
                data = self._data.copy()
                # 仅移除来自环境变量的 api_key（有 api_key_env 的），保留直接配置的
                env_api_keys = {}
                for name, info in data.get("providers", {}).items():
                    if "api_key" in info and info.get("api_key_env", ""):
                        env_api_keys[name] = info.pop("api_key")
                yaml_str = yaml.dump(data, allow_unicode=True, default_flow_style=False)
                # 原子写入：先写临时文件，再 rename 替换（同分区内 rename 是原子的）
                tmp_path = self._config_path.with_suffix(".tmp")
                tmp_path.write_text(yaml_str, encoding="utf-8")
                tmp_path.replace(self._config_path)
                # 恢复 api_key
                for name, key in env_api_keys.items():
                    data["providers"][name]["api_key"] = key

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    @property
    def data(self) -> dict:
        """直接访问原始配置数据（用于管理 API 变更）"""
        return self._data

    def _resolve_path(self, config_path: str | Path | None) -> Path | None:
        if config_path:
            p = Path(config_path)
            if p.is_file():
                return p
            return None
        for p in DEFAULT_CONFIG_PATHS:
            if p.is_file():
                return p
        return None

    def _inject_env(self) -> None:
        """将环境变量中的 API Key 注入 providers 配置"""
        providers = self._data.setdefault("providers", {})
        for name, info in providers.items():
            env_var = info.get("api_key_env", "")
            if env_var:
                env_val = os.environ.get(env_var, "")
                if env_val:
                    info["api_key"] = env_val
                elif "api_key" not in info:
                    info["api_key"] = ""
            elif "api_key" not in info:
                info["api_key"] = ""
        self._normalize_mapping()

    def _normalize_mapping(self) -> None:
        """将旧格式 model_mapping ({alias: target_string}) 迁移到新格式 ({alias: {target, ...}})"""
        mapping = self._data.get("model_mapping", {})
        normalized = {}
        for alias, entry in mapping.items():
            if isinstance(entry, str):
                normalized[alias] = {
                    "target": entry,
                    "provider": "",
                    "enabled": True,
                    "is_multimodal": False,
                    "vision_alias": None,
                    "is_image_gen": False,
                    "image_gen_alias": None,
                    "is_video_gen": False,
                    "video_gen_alias": None,
                }
            elif isinstance(entry, dict):
                entry.setdefault("provider", "")
                entry.setdefault("is_multimodal", False)
                entry.setdefault("vision_alias", None)
                entry.setdefault("is_image_gen", False)
                entry.setdefault("image_gen_alias", None)
                entry.setdefault("is_video_gen", False)
                entry.setdefault("video_gen_alias", None)
                entry.setdefault("enabled", True)
                normalized[alias] = entry
        self._data["model_mapping"] = normalized

    # ── 属性访问 ─────────────────────────────────────────────────

    @property
    def server_host(self) -> str:
        return self._data.get("server", {}).get("host", "127.0.0.1")

    @property
    def server_port(self) -> int:
        return self._data.get("server", {}).get("port", 8765)

    @property
    def vision_routing(self) -> dict:
        return self._data.get("vision_routing", {})

    @property
    def providers(self) -> dict:
        return self._data.get("providers", {})

    @property
    def model_mapping(self) -> dict[str, dict]:
        """模型映射: {alias: {target, provider, is_multimodal, vision_alias}}"""
        return self._data.get("model_mapping", {})

    def get_provider(self, name: str) -> dict | None:
        return self.providers.get(name)

    def _has_api_key(self, provider: dict) -> bool:
        """检查 provider 是否有可用的 API key"""
        if provider.get("api_key", ""):
            return True
        env_var = provider.get("api_key_env", "")
        if env_var and os.environ.get(env_var, ""):
            return True
        return False

    def _enabled_providers(self) -> dict:
        """返回所有启用且有 API key 的 provider"""
        return {k: v for k, v in self.providers.items()
                if v.get("enabled", True) and self._has_api_key(v)}

    def resolve_model(self, model_name: str) -> tuple[str, str]:
        """
        解析 code 模型名 → (provider_name, target_model)

        返回: (provider_name, target_model)
        例如: resolve_model("gpt-5-code") → ("qwen", "qwen-plus")
        """
        # 1. 先查 model_mapping 精确映射（仅启用的条目）
        #    模型明确指定了 provider 时，只检查该 provider 有无 API key，
        #    不检查 provider 级别的 enabled 标志（那个由模型级别 enabled 控制）
        entry = self.model_mapping.get(model_name)
        if isinstance(entry, dict) and entry.get("enabled", True):
            target = entry.get("target", model_name)
            provider_name = entry.get("provider", "")
            if not provider_name:
                provider_name = self._find_provider_for_target(target)
            if provider_name and provider_name in self.providers:
                p = self.providers[provider_name]
                if self._has_api_key(p):
                    return provider_name, target

        # 2. 模糊匹配 provider 名（仅启用且有 API key 的 provider）
        for pname, pinfo in self.providers.items():
            if pinfo.get("enabled", True) and self._has_api_key(pinfo) and pname in model_name.lower():
                return pname, self._get_default_model(pname)

        # 3. 返回第一个启用且有 API key 的 provider
        enabled = self._enabled_providers()
        if enabled:
            first = next(iter(enabled.items()))
            return first[0], self._get_default_model(first[0])
        return "unknown", model_name

    def _find_provider_for_target(self, target: str) -> str | None:
        """根据 target 名查找对应的 provider（仅查找有 API key 的）"""
        for pname, pinfo in self.providers.items():
            if not self._has_api_key(pinfo):
                continue
            if pinfo.get("adapter") == target or pname == target:
                return pname
        for pname, pinfo in self.providers.items():
            if not self._has_api_key(pinfo):
                continue
            if pname in target.lower():
                return pname
        enabled = self._enabled_providers()
        return next(iter(enabled), None) if enabled else None

    def _get_default_model(self, provider_name: str) -> str:
        """获取 provider 的默认模型名，取 mapping 中第一个匹配的"""
        mapping = self.model_mapping
        pname = provider_name
        for alias, entry in mapping.items():
            target = entry.get("target", entry) if isinstance(entry, dict) else entry
            found = self._find_provider_for_target(target)
            if found == pname:
                return target
        return pname  # fallback

    # ── 生成默认配置 ─────────────────────────────────────────────

    @staticmethod
    def generate_default(output_path: Path) -> None:
        """生成默认配置文件"""
        default = {
            "server": {"host": "127.0.0.1", "port": 8765},
            "providers": {
                "qwen": {
                    "adapter": "qwen",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "api_key_env": "QWEN_API_KEY",
                },
                "deepseek": {
                    "adapter": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key_env": "DEEPSEEK_API_KEY",
                },
                "kimi": {
                    "adapter": "kimi",
                    "base_url": "https://api.moonshot.cn/v1",
                    "api_key_env": "KIMI_API_KEY",
                },
            },
            "model_mapping": {
                "gpt-5-code": {"target": "qwen-plus", "provider": "qwen", "is_multimodal": False, "vision_alias": None},
                "gpt-5-code-light": {"target": "qwen-turbo", "provider": "qwen", "is_multimodal": False, "vision_alias": None},
                "gpt-5": {"target": "qwen-plus", "provider": "qwen", "is_multimodal": False, "vision_alias": None},
            },
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml.dump(default, allow_unicode=True, default_flow_style=False), encoding="utf-8")


# 全局单例
_config_instance: Config | None = None


def get_config(config_path: str | Path | None = None) -> Config:
    global _config_instance
    if _config_instance is None:
        _config_instance = Config(config_path)
    return _config_instance


def reload_config() -> None:
    cfg = get_config()
    cfg.reload()
