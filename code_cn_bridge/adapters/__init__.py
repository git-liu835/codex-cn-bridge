"""适配器注册表 —— 模型名到适配器实例的映射管理"""

from __future__ import annotations

from .base import BaseAdapter


class AdapterRegistry:
    """适配器注册表"""

    def __init__(self):
        self._adapters: dict[str, BaseAdapter] = {}

    def register(self, adapter: BaseAdapter) -> None:
        """注册一个适配器"""
        self._adapters[adapter.name] = adapter

    def register_all(self) -> None:
        """注册所有内置适配器（便于外部显式调用）"""
        _register_builtins(self)

    def get(self, name: str) -> BaseAdapter | None:
        """按名称获取适配器"""
        return self._adapters.get(name)

    def list(self) -> list[str]:
        """列出所有已注册的适配器名称"""
        return list(self._adapters.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._adapters


# 全局注册表
_registry: AdapterRegistry | None = None


def get_registry() -> AdapterRegistry:
    global _registry
    if _registry is None:
        _registry = AdapterRegistry()
        _register_builtins(_registry)
    return _registry


def _register_builtins(reg: AdapterRegistry) -> None:
    """注册内置适配器"""
    from .qwen import QwenAdapter
    from .deepseek import DeepSeekAdapter
    from .kimi import KimiAdapter
    from .doubao import DoubaoAdapter
    from .glm import GlmAdapter
    from .agnes import AgnesAdapter
    from .minimax import MiniMaxAdapter
    from .hunyuan import HunyuanAdapter
    from .ernie import ErnieAdapter
    from .spark import SparkAdapter
    from .siliconflow import SiliconFlowAdapter
    from .ollama import OllamaAdapter

    reg.register(QwenAdapter())
    reg.register(DeepSeekAdapter())
    reg.register(KimiAdapter())
    reg.register(DoubaoAdapter())
    reg.register(GlmAdapter())
    reg.register(AgnesAdapter())
    reg.register(MiniMaxAdapter())
    reg.register(HunyuanAdapter())
    reg.register(ErnieAdapter())
    reg.register(SparkAdapter())
    reg.register(SiliconFlowAdapter())
    reg.register(OllamaAdapter())
