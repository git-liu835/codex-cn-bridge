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


def _read_codex_threads(limit: int = 10) -> str:
    """从 Codex 本地数据库读取最近的历史线程摘要

    读取 ~/.codex/state_5.sqlite 中的 threads 表，
    提取最近 N 条线程的标题、首条消息和预览，作为跨会话记忆。
    """
    import sqlite3
    codex_db = Path.home() / ".codex" / "state_5.sqlite"
    if not codex_db.is_file():
        return ""

    try:
        conn = sqlite3.connect(f"file:{codex_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            """SELECT title, first_user_message, preview, cwd,
                      datetime(created_at, 'unixepoch', 'localtime') AS created,
                      datetime(updated_at, 'unixepoch', 'localtime') AS updated
               FROM threads
               WHERE archived = 0 AND title != ''
               ORDER BY updated_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = c.fetchall()
        conn.close()

        if not rows:
            return ""

        lines = [f"[Codex 历史会话 — 最近 {len(rows)} 条]"]
        for i, row in enumerate(rows, 1):
            title = row["title"] or "(无标题)"
            first_msg = (row["first_user_message"] or "")[:200]
            preview = (row["preview"] or "")[:300]
            proj = (row["cwd"] or "").replace("\\\\?\\", "")
            lines.append(
                f"\n## 会话 {i}: {title}"
                f"\n   项目: {proj}"
                f"\n   时间: {row['updated']}"
            )
            if first_msg:
                lines.append(f"   首条消息: {first_msg}")
            if preview:
                lines.append(f"   摘要: {preview}")

        return "\n".join(lines)
    except Exception:
        return ""


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
                # 仅移除来自环境变量的 api_key/api_keys，保留用户手动配置的
                env_api_keys = {}
                env_api_keys_list = {}
                for name, info in data.get("providers", {}).items():
                    if info.pop("_api_key_from_env", False):
                        env_api_keys[name] = info.pop("api_key", "")
                    if info.pop("_api_keys_from_env", False):
                        env_api_keys_list[name] = info.pop("api_keys", [])
                yaml_str = yaml.dump(data, allow_unicode=True, default_flow_style=False)
                # 原子写入：先写临时文件，再 rename 替换（同分区内 rename 是原子的）
                tmp_path = self._config_path.with_suffix(".tmp")
                tmp_path.write_text(yaml_str, encoding="utf-8")
                tmp_path.replace(self._config_path)
                # 恢复 api_key/api_keys
                for name, key in env_api_keys.items():
                    data["providers"][name]["api_key"] = key
                for name, keys in env_api_keys_list.items():
                    data["providers"][name]["api_keys"] = keys

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
        """将环境变量中的 API Key 注入 providers 配置

        支持三种配置方式:
        - api_key_env: 单个环境变量名 → api_key
        - api_keys_env: 逗号分隔的多个环境变量名 → api_keys 列表
        - api_keys: 直接配置的 key 列表（优先级最低）
        """
        providers = self._data.setdefault("providers", {})
        for name, info in providers.items():
            # 列表形式: api_keys_env (逗号分隔的环境变量名)
            keys_env_str = info.get("api_keys_env", "")
            if keys_env_str:
                keys = []
                for env_var in keys_env_str.split(","):
                    env_var = env_var.strip()
                    env_val = os.environ.get(env_var, "")
                    if env_val:
                        keys.append(env_val)
                if keys:
                    info["api_keys"] = keys
                    info["_api_keys_from_env"] = True

            # 单 key 形式: api_key_env
            env_var = info.get("api_key_env", "")
            if env_var:
                env_val = os.environ.get(env_var, "")
                if env_val:
                    info["api_key"] = env_val
                    info["_api_key_from_env"] = True
                elif "api_key" not in info and "api_keys" not in info:
                    info["api_key"] = ""
            elif "api_key" not in info and "api_keys" not in info:
                info["api_key"] = ""
        self._normalize_mapping()

    def _normalize_mapping(self) -> None:
        """将旧格式 model_mapping ({alias: target_string}) 迁移到新格式 ({alias: {target, ...}} 或列表)

        支持三种格式：
        - 旧格式: alias → target_string
        - 单模型: alias → {target, provider, ...}
        - 多模型: alias → [{target, provider, ...}, ...]  (同名多个后端，仅一个 enabled)
        """
        mapping = self._data.get("model_mapping", {})
        normalized = {}

        def _norm_entry(entry: dict) -> dict:
            entry.setdefault("provider", "")
            entry.setdefault("is_multimodal", False)
            entry.setdefault("vision_alias", None)
            entry.setdefault("is_image_gen", False)
            entry.setdefault("image_gen_alias", None)
            entry.setdefault("is_video_gen", False)
            entry.setdefault("video_gen_alias", None)
            entry.setdefault("enabled", True)
            entry.setdefault("enable_thinking", True)
            entry.setdefault("thinking_budget", 4096)
            return entry

        for alias, entry in mapping.items():
            if isinstance(entry, str):
                normalized[alias] = _norm_entry({
                    "target": entry,
                    "provider": "",
                    "enabled": True,
                })
            elif isinstance(entry, list):
                # 多模型列表：规范化每个条目，确保仅一个 enabled
                items = [_norm_entry(e) for e in entry]
                enabled_count = sum(1 for e in items if e.get("enabled"))
                if enabled_count == 0 and items:
                    items[0]["enabled"] = True
                elif enabled_count > 1:
                    first = True
                    for e in items:
                        if e.get("enabled"):
                            if first:
                                first = False
                            else:
                                e["enabled"] = False
                normalized[alias] = items
            elif isinstance(entry, dict):
                normalized[alias] = _norm_entry(entry)
        self._data["model_mapping"] = normalized

    # ── 属性访问 ─────────────────────────────────────────────────

    @property
    def server_host(self) -> str:
        return self._data.get("server", {}).get("host", "127.0.0.1")

    @property
    def server_port(self) -> int:
        return self._data.get("server", {}).get("port", 8765)

    @property
    def verbose_log(self) -> bool:
        return self._data.get("server", {}).get("verbose_log", False)

    @property
    def max_context_tokens(self) -> int:
        return self._data.get("server", {}).get("max_context_tokens", 131072)

    @property
    def response_cache_size(self) -> int:
        return self._data.get("server", {}).get("response_cache_size", 500)

    @property
    def project_context(self) -> str:
        """持久化项目上下文，每次请求注入到 system 消息中。

        配置方式（~/.code-cn-bridge.yaml）:
          context:
            project_dir: "G:\\\\path\\\\to\\\\project"      # 项目目录（自动检测 CODEX.md 等）
            rules_file: "G:\\\\path\\\\to\\\\CODEX.md"      # 规则文件（显式指定）
            project_prompt: "项目简介文本"                    # 直接文本
            memory_file: "~/.code-cn-bridge/memory/proj.json"  # 跨会话记忆
            codex_history: 10                                # 读取 Codex 历史线程数
        """
        ctx = self._data.get("context", {})
        parts: list[str] = []

        # 直接文本注入
        prompt = ctx.get("project_prompt", "").strip()
        if prompt:
            parts.append(prompt)

        # 从规则文件读取（显式指定或自动检测 CODEX.md / CLAUDE.md 等）
        rules_file = ctx.get("rules_file", "").strip()
        if rules_file:
            try:
                p = Path(os.path.expandvars(os.path.expanduser(rules_file)))
                if p.is_file():
                    content = p.read_text(encoding="utf-8")[:16384]
                    parts.append(f"[项目规则文件: {p.name}]\n{content}")
            except Exception:
                pass
        else:
            # 自动检测：在 project_dir 下查找 CODEX.md、CLAUDE.md、.cursorrules、CODEBUDDY.md
            project_dir = ctx.get("project_dir", "").strip()
            if not project_dir:
                project_dir = os.getenv("CODE_PROJECT_DIR", "").strip()
            if project_dir:
                try:
                    pdir = Path(os.path.expandvars(os.path.expanduser(project_dir)))
                    if pdir.is_dir():
                        for name in ("CODEX.md", "CLAUDE.md", ".cursorrules", "CODEBUDDY.md", "AGENTS.md"):
                            fpath = pdir / name
                            if fpath.is_file():
                                content = fpath.read_text(encoding="utf-8")[:16384]
                                parts.append(f"[项目规则文件: {name}]\n{content}")
                                break  # 只读第一个找到的
                except Exception:
                    pass

        # 从跨会话记忆文件读取
        memory_file = ctx.get("memory_file", "").strip()
        if memory_file:
            try:
                p = Path(os.path.expandvars(os.path.expanduser(memory_file)))
                if p.is_file():
                    mem = p.read_text(encoding="utf-8")
                    if mem.strip():
                        parts.append(f"[项目持久记忆]\n{mem.strip()}")
            except Exception:
                pass

        # 从 Codex 本地数据库读取历史线程摘要
        thread_count = ctx.get("codex_history", 0)
        if isinstance(thread_count, int) and thread_count > 0:
            try:
                codex_threads = _read_codex_threads(thread_count)
                if codex_threads:
                    parts.append(codex_threads)
            except Exception:
                pass

        return "\n\n".join(parts)

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
        """检查 provider 是否有可用的 API key（支持单 key 和多 key 列表）"""
        if provider.get("api_key", ""):
            return True
        keys = provider.get("api_keys", [])
        if keys:
            return True
        env_var = provider.get("api_key_env", "")
        if env_var and os.environ.get(env_var, ""):
            return True
        return False

    def get_api_keys(self, provider_name: str) -> list[str]:
        """获取 provider 的所有 API key（支持多 key 轮转）

        返回 API key 列表，至少包含一个元素（可能为空字符串）。
        优先使用 api_keys 列表，回退到单 api_key。
        """
        provider = self.providers.get(provider_name, {})
        keys = provider.get("api_keys", [])
        if keys:
            return [k for k in keys if k]
        single = provider.get("api_key", "")
        if single:
            return [single]
        # 再次检查环境变量
        env_var = provider.get("api_key_env", "")
        if env_var:
            env_val = os.environ.get(env_var, "")
            if env_val:
                return [env_val]
        return [""]

    def _next_key_index(self, provider_name: str, current_index: int = -1) -> tuple[str, int]:
        """轮转到下一个 API key，返回 (key, new_index)"""
        keys = self.get_api_keys(provider_name)
        if not keys or not keys[0]:
            return "", 0
        idx = (current_index + 1) % len(keys)
        return keys[idx], idx

    def _enabled_providers(self) -> dict:
        """返回所有启用且有 API key 的 provider"""
        return {k: v for k, v in self.providers.items()
                if v.get("enabled", True) and self._has_api_key(v)}

    def resolve_model(self, model_name: str) -> tuple[str, str]:
        """
        解析 code 模型名 → (provider_name, target_model)

        返回: (provider_name, target_model)
        例如: resolve_model("gpt-5-code") → ("qwen", "qwen-plus")

        支持多模型列表：当 alias 映射到列表时，使用第一个 enabled 的条目。
        """
        # 辅助：从单个条目查找 provider
        def _resolve_entry(entry: dict) -> tuple[str, str] | None:
            target = entry.get("target", model_name)
            provider_name = entry.get("provider", "")
            if not provider_name:
                provider_name = self._find_provider_for_target(target)
            if provider_name and provider_name in self.providers:
                p = self.providers[provider_name]
                if self._has_api_key(p):
                    return provider_name, target
            return None

        # 1. 先查 model_mapping 精确映射（仅启用的条目）
        entry = self.model_mapping.get(model_name)
        if isinstance(entry, list):
            for item in entry:
                if item.get("enabled", True):
                    result = _resolve_entry(item)
                    if result:
                        return result
        elif isinstance(entry, dict) and entry.get("enabled", True):
            result = _resolve_entry(entry)
            if result:
                return result

        # 1b. 按 target 模型名反向查找（Codex 选择真实模型名时走这条路径）
        for alias, entry in self.model_mapping.items():
            items = entry if isinstance(entry, list) else [entry]
            for item in items:
                if not item.get("enabled", True):
                    continue
                if item.get("target") == model_name:
                    result = _resolve_entry(item)
                    if result:
                        return result

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
        """获取 provider 的默认模型名，取 mapping 中第一个匹配的（支持多模型列表）"""
        mapping = self.model_mapping
        for alias, entry in mapping.items():
            if isinstance(entry, list):
                for item in entry:
                    target = item.get("target", alias)
                    found = self._find_provider_for_target(target)
                    if found == provider_name:
                        return target
            elif isinstance(entry, dict):
                target = entry.get("target", alias)
                found = self._find_provider_for_target(target)
                if found == provider_name:
                    return target
        return provider_name  # fallback

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
