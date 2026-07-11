"""厂商预设与模型元数据 —— 统一 base_url / 模型列表 / 上下文窗口

桌面端「厂商卡片」、Codex catalog、/v1/models、上下文压缩共用此表，
避免多处硬编码不一致。
"""

from __future__ import annotations

# 默认上下文（未知模型）
DEFAULT_CONTEXT_WINDOW = 200_000

# 压缩触发比例：接近该比例时自动压缩旧对话
CONTEXT_COMPRESS_RATIO = 0.85
# 硬截断比例：压缩后仍超限则截断
CONTEXT_TRUNCATE_RATIO = 0.95


def estimate_context_window(
    alias: str = "",
    target: str = "",
    provider: str = "",
    explicit: int | None = None,
) -> int:
    """估算模型上下文窗口（tokens）。

    优先使用配置里显式的 context_window；否则按模型名/厂商推断。
    """
    if explicit and explicit > 0:
        return int(explicit)

    text = f"{alias} {target} {provider}".lower()

    # DeepSeek V4：官方 1M
    if "deepseek-v4" in text or (
        "deepseek" in text and ("v4-pro" in text or "v4-flash" in text or "v4_pro" in text)
    ):
        return 1_000_000
    if provider == "deepseek" or "deepseek" in text:
        return 1_000_000

    if "kimi" in text or "moonshot" in text:
        return 2_000_000
    if "minimax" in text:
        return 1_000_000
    if "qwen" in text or "dashscope" in text:
        return 256_000
    if "doubao" in text or "seed" in text:
        return 256_000
    if "glm" in text or "zhipu" in text:
        return 200_000
    if "ernie" in text or "speed-pro" in text:
        return 128_000
    if "spark" in text:
        return 128_000
    if "hunyuan" in text:
        return 256_000
    if "agnes" in text:
        return 128_000
    if "siliconflow" in text or "silicon" in text:
        return 128_000
    if "ollama" in text or "lmstudio" in text:
        return 8192
    if "claude" in text or "anthropic" in text:
        return 200_000
    if "gpt-5" in text or "openai" in text:
        return 256_000
    if "gemini" in text:
        return 1_000_000
    if "grok" in text:
        return 256_000

    return DEFAULT_CONTEXT_WINDOW


# 内置 provider 预设：添加卡片时自动填充，并一次性写入整家模型列表
# models 中每项会成为 Codex 可切换的独立 alias（slug = 模型 id）
PROVIDER_PRESETS: list[dict] = [
    # ═══ 国内厂商 ═══
    {
        "name": "deepseek",
        "label": "DeepSeek 深度求索",
        "adapter": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "docs_url": "https://platform.deepseek.com/api_keys",
        # 仅 V4；deepseek-chat / reasoner 将于 2026-07-24 下线
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "context_window": 1_000_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    {
        "name": "zhipu",
        "label": "智谱 GLM",
        "adapter": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY",
        "docs_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "models": ["glm-5.2", "glm-5.1", "glm-5", "glm-4.7", "glm-4.7-flash"],
        "context_window": 200_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    {
        "name": "qwen",
        "label": "通义千问 阿里云",
        "adapter": "qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "QWEN_API_KEY",
        "docs_url": "https://bailian.console.aliyun.com/?apiKey=1#/api-key",
        "models": [
            "qwen3-coder-plus",
            "qwen3-coder-next",
            "qwen3.5-max",
            "qwen3-max",
            "qwen3-coder-480b-a35b-instruct",
        ],
        "context_window": 256_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    {
        "name": "kimi",
        "label": "Kimi 月之暗面",
        "adapter": "kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "KIMI_API_KEY",
        "docs_url": "https://platform.moonshot.cn/console/api-keys",
        "models": ["kimi-k2.6", "kimi-k2.5", "kimi-k2", "kimi-k2.7-code"],
        "context_window": 2_000_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    {
        "name": "doubao",
        "label": "豆包 字节跳动",
        "adapter": "doubao",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key_env": "ARK_API_KEY",
        "docs_url": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "models": ["doubao-seed-2-0", "doubao-seed-1-8", "doubao-pro-1-5"],
        "context_window": 256_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    {
        "name": "ernie",
        "label": "文心一言 百度",
        "adapter": "ernie",
        "base_url": "https://qianfan.baidubce.com/v2",
        "api_key_env": "ERNIE_API_KEY",
        "docs_url": "https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
        "models": ["ernie-5.1", "ernie-speed-pro-128k"],
        "context_window": 128_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    {
        "name": "hunyuan",
        "label": "混元 腾讯",
        "adapter": "hunyuan",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "api_key_env": "HUNYUAN_API_KEY",
        "docs_url": "https://console.cloud.tencent.com/hunyuan/api-key",
        "models": ["hunyuan-turbos-latest", "hunyuan-t1-latest"],
        "context_window": 256_000,
        "enable_thinking": False,
        "region": "domestic",
    },
    {
        "name": "minimax",
        "label": "MiniMax",
        "adapter": "minimax",
        "base_url": "https://api.minimaxi.com/v1",
        "api_key_env": "MINIMAX_API_KEY",
        "docs_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
        "models": ["MiniMax-M3", "MiniMax-M2.7"],
        "context_window": 1_000_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    {
        "name": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "adapter": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "SILICONFLOW_API_KEY",
        "docs_url": "https://cloud.siliconflow.cn/account/ak",
        "models": [
            "deepseek-ai/DeepSeek-V4-Pro",
            "deepseek-ai/DeepSeek-V4-Flash",
            "Qwen/Qwen3-Coder-Next",
            "Pro/zai-org/GLM-5",
        ],
        "context_window": 128_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    {
        "name": "spark",
        "label": "讯飞星火",
        "adapter": "spark",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "api_key_env": "SPARK_API_KEY",
        "docs_url": "https://console.xfyun.cn/services/bm4",
        "models": ["generalv3.5", "4.0Ultra", "max-32k"],
        "context_window": 128_000,
        "enable_thinking": False,
        "region": "domestic",
    },
    {
        "name": "agnes",
        "label": "Agnes 聚合",
        "adapter": "agnes",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY",
        "docs_url": "",
        "models": [
            "agnes-2.0-flash",
            "agnes-1.5-flash",
            "agnes-image-2.1-flash",
            "agnes-video-v2.0",
        ],
        "context_window": 128_000,
        "enable_thinking": True,
        "region": "domestic",
    },
    # ═══ 本地 ═══
    {
        "name": "ollama",
        "label": "Ollama 本地",
        "adapter": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": "OLLAMA_API_KEY",
        "docs_url": "https://ollama.com",
        "models": ["qwen3:latest", "deepseek-v4:latest", "llama3:latest"],
        "context_window": 8192,
        "enable_thinking": False,
        "region": "local",
    },
    {
        "name": "lmstudio",
        "label": "LM Studio 本地",
        "adapter": "ollama",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key_env": "LMSTUDIO_API_KEY",
        "docs_url": "https://lmstudio.ai",
        "models": ["local-model"],
        "context_window": 8192,
        "enable_thinking": False,
        "region": "local",
    },
    # ═══ 国外 / 聚合（透传 OpenAI 兼容）═══
    {
        "name": "openrouter",
        "label": "OpenRouter 聚合",
        "adapter": "qwen",  # OpenAI 兼容透传
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "docs_url": "https://openrouter.ai/keys",
        "models": [
            "deepseek/deepseek-v4-pro",
            "anthropic/claude-sonnet-4.5",
            "openai/gpt-5.4",
        ],
        "context_window": 200_000,
        "enable_thinking": True,
        "region": "overseas",
    },
]


def get_preset(name: str) -> dict | None:
    for p in PROVIDER_PRESETS:
        if p["name"] == name:
            return p
    return None


def list_presets_public() -> list[dict]:
    """返回给前端的预设列表（去掉内部-only 字段亦可直接返回）"""
    return [
        {
            "name": p["name"],
            "label": p["label"],
            "adapter": p["adapter"],
            "base_url": p["base_url"],
            "api_key_env": p["api_key_env"],
            "docs_url": p.get("docs_url", ""),
            "models": list(p.get("models", [])),
            "context_window": p.get("context_window", DEFAULT_CONTEXT_WINDOW),
            "enable_thinking": p.get("enable_thinking", True),
            "region": p.get("region", "domestic"),
        }
        for p in PROVIDER_PRESETS
    ]
