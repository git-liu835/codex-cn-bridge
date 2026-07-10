"""生成 Codex 桌面端的 model catalog JSON 文件。

Codex 桌面端通过 config.toml 的 `model_catalog_json` 字段读取一个 JSON 文件，
该文件决定了 Codex 输入框中模型下拉列表的所有选项（CC Switch 的核心做法）。

本模块根据桥接器的 config.yaml 生成对应的 catalog 文件，
让 Codex 桌面端只显示桥接器配置的模型。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import get_config

_logger = logging.getLogger(__name__)

# Codex 桌面端 catalog 文件的默认路径（与 CC Switch 一致）
CODEX_HOME = Path.home() / ".codex"
DEFAULT_CATALOG_PATH = CODEX_HOME / "code-cn-bridge-catalog.json"

# CC Switch 生成的 catalog 文件路径（用于复用 base_instructions 等模板字段）
CC_SWITCH_CATALOG_PATH = CODEX_HOME / "cc-switch-model-catalog.json"


def _load_template_fields() -> dict[str, Any]:
    """从 CC Switch 的 catalog 文件加载模板字段（base_instructions, model_messages 等）。

    这些字段内容很长（约 20KB），是 Codex 桌面端必需的。
    直接复用 CC Switch 已生成的模板，避免硬编码。
    """
    if not CC_SWITCH_CATALOG_PATH.exists():
        _logger.warning("CC Switch catalog 不存在: %s，使用最小模板", CC_SWITCH_CATALOG_PATH)
        return {
            "base_instructions": "You are Codex, a coding agent.",
            "model_messages": {
                "instructions_template": "You are Codex, a coding agent.",
                "instructions_variables": {},
            },
        }

    try:
        data = json.loads(CC_SWITCH_CATALOG_PATH.read_text(encoding="utf-8"))
        if not data.get("models"):
            raise ValueError("CC Switch catalog 无模型")
        m = data["models"][0]
        return {
            "base_instructions": m.get("base_instructions", ""),
            "model_messages": m.get("model_messages", {}),
        }
    except Exception as e:
        _logger.warning("读取 CC Switch catalog 失败: %s，使用最小模板", e)
        return {
            "base_instructions": "You are Codex, a coding agent.",
            "model_messages": {
                "instructions_template": "You are Codex, a coding agent.",
                "instructions_variables": {},
            },
        }


def _build_model_entry(alias: str, item: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """根据桥接器的模型配置生成一个 catalog 条目。

    参考 CC Switch 生成的 cc-switch-model-catalog.json 格式，
    复用 CC Switch 的 base_instructions 和 model_messages 模板字段。
    """
    target = item.get("target", alias)
    provider_name = item.get("provider", "cn-bridge")
    is_mm = item.get("is_multimodal", False)
    is_img = item.get("is_image_gen", False)
    is_vid = item.get("is_video_gen", False)
    is_thinking = item.get("enable_thinking", False)

    # 估算上下文窗口
    ctx = 200000
    alias_lower = alias.lower()
    target_lower = target.lower()
    if "kimi" in alias_lower or "kimi" in target_lower:
        ctx = 2000000
    elif "minimax" in alias_lower or "minimax" in target_lower:
        ctx = 1000000
    elif "qwen" in alias_lower or "qwen" in target_lower:
        ctx = 256000
    elif "doubao" in alias_lower or "doubao" in target_lower:
        ctx = 256000
    elif "ernie" in alias_lower or "speed-pro" in alias_lower:
        ctx = 128000
    elif "spark" in alias_lower or "spark" in target_lower:
        ctx = 128000
    elif "ollama" in alias_lower or "ollama" in target_lower:
        ctx = 8192

    # 构建展示名称：alias + 真实模型信息
    display_name = alias
    if target and target != alias:
        display_name = f"{alias} ({target})"

    # 输入模态
    input_modalities = ["text"]
    if is_mm:
        input_modalities.append("image")

    entry = {
        # 必需字段（Codex 桌面端解析时要求存在）
        "base_instructions": template.get("base_instructions", ""),
        "model_messages": template.get("model_messages", {}),
        # 模型标识
        "slug": alias,
        "display_name": display_name,
        "description": f"{provider_name}/{target}",
        # 上下文窗口
        "context_window": ctx,
        "max_context_window": ctx,
        "effective_context_window_percent": 95,
        # 推理与输出
        "default_reasoning_level": "medium" if is_thinking else "low",
        "default_reasoning_summary": "none",
        "default_verbosity": "low",
        # 列表显示
        "priority": 1000,
        "visibility": "list",
        "supported_in_api": True,
        "support_verbosity": True,
        # 工具支持
        "supports_parallel_tool_calls": not is_img and not is_vid,
        "supports_reasoning_summaries": is_thinking,
        "supports_search_tool": True,
        "supports_image_detail_original": is_mm,
        "shell_type": "shell_command",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        # 模态
        "input_modalities": input_modalities,
        # 其他
        "experimental_supported_tools": [],
        "additional_speed_tiers": [],
        "service_tiers": [],
        "supported_reasoning_levels": [
            {"description": "快速响应，轻量推理", "effort": "low"},
            {"description": "平衡速度和推理深度", "effort": "medium"},
            {"description": "更深的推理，适合复杂问题", "effort": "high"},
        ],
        "truncation_policy": {"limit": 10000, "mode": "tokens"},
        "upgrade": None,
        "availability_nux": None,
    }
    return entry


def generate_catalog(catalog_path: Path | None = None) -> Path:
    """根据当前 config 生成 Codex 桌面端的 model catalog JSON 文件。

    Args:
        catalog_path: catalog 文件路径，默认为 ~/.codex/code-cn-bridge-catalog.json

    Returns:
        生成的 catalog 文件路径
    """
    if catalog_path is None:
        catalog_path = DEFAULT_CATALOG_PATH

    cfg = get_config()
    models = []

    # 加载 CC Switch 的模板字段（base_instructions 等）
    template = _load_template_fields()

    for alias, entry in cfg.model_mapping.items():
        # 支持多模型列表：至少一个条目启用就算启用
        items = entry if isinstance(entry, list) else [entry]
        if not any(item.get("enabled", True) for item in items):
            continue
        for item in items:
            if not item.get("enabled", True):
                continue
            models.append(_build_model_entry(alias, item, template))
            break  # 每个 alias 只取第一个启用的条目

    catalog = {"models": models}

    # 确保目录存在
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _logger.info(
        "已生成 Codex 桌面端 model catalog: %s (共 %d 个模型)",
        catalog_path,
        len(models),
    )
    return catalog_path


def update_codex_config(catalog_path: Path | None = None) -> bool:
    """更新 Codex 的 config.toml，设置 model_catalog_json 指向生成的 catalog 文件。

    采用 CC Switch v3.16.4 的段级增量编辑 + 原子写入机制：
    - **段级处理**：只在 config.toml 的顶层（非 [section] 段内）查找/替换
      model_catalog_json，避免误改 [model_providers.xxx] 等子段
    - **折叠重复**：保留第一处匹配并替换，移除其余重复条目
      （参考 CC Switch PR #4316 修复重复 base_url 的做法）
    - **原子写入**：临时文件 + os.replace，防止写入过程中崩溃导致配置文件损坏
      （参考 CC Switch codex-official-auth-preservation-guide 推荐做法）

    Args:
        catalog_path: catalog 文件路径，默认为 ~/.codex/code-cn-bridge-catalog.json

    Returns:
        True 如果成功更新，False 如果失败
    """
    if catalog_path is None:
        catalog_path = DEFAULT_CATALOG_PATH

    config_path = CODEX_HOME / "config.toml"
    if not config_path.exists():
        _logger.warning("Codex config.toml 不存在: %s", config_path)
        return False

    content = config_path.read_text(encoding="utf-8")
    catalog_line = f'model_catalog_json = "{catalog_path.name}"'

    lines = content.splitlines(keepends=True)  # 保留换行符以保持原格式
    new_lines: list[str] = []
    inserted = False
    found_duplicate = False
    in_section = False  # 是否在 [section] 段内

    # 第一遍：处理已有 model_catalog_json（段级感知 + 折叠重复）
    for line in lines:
        stripped = line.strip()

        # 检测段开始：[xxx] 或 [xxx.yyy]
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            in_section = True
            new_lines.append(line)
            continue

        # 只在顶层（非段内）处理 model_catalog_json
        if not in_section and stripped.startswith("model_catalog_json"):
            if not inserted:
                # 替换第一处
                # 保留原行的换行符（如果原行有换行）
                line_ending = "\n" if line.endswith("\n") else ""
                new_lines.append(catalog_line + line_ending)
                inserted = True
            else:
                # 折叠重复条目：跳过（不追加）
                found_duplicate = True
                _logger.info("折叠重复的 model_catalog_json 条目")
            continue

        new_lines.append(line)

    # 第二遍：如果没有已有的 model_catalog_json，在顶层合适位置插入
    if not inserted:
        new_lines = []
        in_section = False
        # 优先在 model_provider 行后插入；其次在 model 行后；最后在首段 [ 前
        for line in lines:
            new_lines.append(line)
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
                in_section = True
                continue
            if not in_section and not inserted:
                if stripped.startswith("model_provider") or stripped.startswith("model "):
                    # 保留原行的换行符
                    line_ending = "\n" if line.endswith("\n") else ""
                    new_lines.append(catalog_line + line_ending)
                    inserted = True

        # 如果还没有插入，在第一个 [section] 之前插入
        if not inserted:
            new_lines = []
            in_section = False
            for line in lines:
                stripped = line.strip()
                if not inserted and stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
                    # 在第一个段之前插入
                    new_lines.append(catalog_line + "\n")
                    inserted = True
                new_lines.append(line)

    if not inserted:
        # 最后回退：追加到文件末尾
        new_lines.append(catalog_line + "\n")
        inserted = True

    new_content = "".join(new_lines)

    # 原子写入：临时文件 + os.replace
    # 防止写入过程中崩溃导致 config.toml 损坏
    try:
        config_dir = config_path.parent
        config_dir.mkdir(parents=True, exist_ok=True)
        # 在同目录创建临时文件（os.replace 在同目录下是原子的）
        fd, tmp_path = tempfile.mkstemp(
            prefix=".code-cn-bridge-config-",
            suffix=".tmp",
            dir=str(config_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)
        except Exception:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        _logger.error("原子写入 config.toml 失败: %s", e)
        return False

    _logger.info("已更新 Codex config.toml (原子写入): model_catalog_json = %s%s",
                 catalog_path.name,
                 "，折叠了重复条目" if found_duplicate else "")
    return True


# ═══════════════════════════════════════════════════════════════════
# Codex 配置模式切换（参考 CC Switch 的配置切换器）
# ═══════════════════════════════════════════════════════════════════

# 桥接器 provider 名（与 /codex-config 端点生成的一致）
BRIDGE_PROVIDER_NAME = "code-cn-bridge"

# 桥接器模式写入 config.toml 的顶层字段（用于检测和清理）
_BRIDGE_TOP_KEYS = (
    "model_provider",
    "model_catalog_json",
    "model",
    "model_reasoning_effort",
)


def _is_bridge_provider_line(stripped: str) -> bool:
    """判断一行是否是桥接器的 provider 声明"""
    return (
        stripped.startswith("model_provider")
        and BRIDGE_PROVIDER_NAME in stripped
    )


def _is_bridge_catalog_line(stripped: str) -> bool:
    """判断一行是否是桥接器的 catalog 声明"""
    return (
        stripped.startswith("model_catalog_json")
        and "code-cn-bridge" in stripped
    )


def get_codex_mode() -> str:
    """检测当前 Codex 配置模式

    通过读取 config.toml 顶层字段判断：
    - "bridge"：存在 model_provider = "code-cn-bridge" 或 model_catalog_json
    - "official"：无桥接器字段，使用官方 ChatGPT 账号

    Returns:
        "bridge" 或 "official"
    """
    config_path = CODEX_HOME / "config.toml"
    if not config_path.exists():
        return "official"

    try:
        content = config_path.read_text(encoding="utf-8")
    except Exception:
        return "official"

    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        # 段开始后跳过（只检查顶层字段）
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            in_section = True
            continue
        if not in_section:
            if _is_bridge_provider_line(stripped) or _is_bridge_catalog_line(stripped):
                return "bridge"
    return "official"


def _collect_enabled_models(cfg) -> list[tuple[str, str, str]]:
    """收集已启用的模型列表（与 /codex-config 端点逻辑一致）

    Returns:
        [(alias, target, provider), ...]
    """
    enabled = []
    for alias, entry in cfg.model_mapping.items():
        items = entry if isinstance(entry, list) else [entry]
        for item in items:
            if item.get("enabled", True):
                target = item.get("target", alias)
                provider = item.get("provider", "cn-bridge")
                enabled.append((alias, target, provider))
                break
    return enabled


def _pick_default_model(enabled_models: list[tuple[str, str, str]]) -> str | None:
    """选择默认模型（与 /codex-config 端点逻辑一致）"""
    if not enabled_models:
        return None
    code_aliases = {"deepseek-v4", "gpt-5-code", "kimi-k2-7-code", "qwen3-coder-plus"}
    for alias, _, _ in enabled_models:
        if alias in code_aliases:
            return alias
    return enabled_models[0][0]


def _atomic_write_config(config_path: Path, new_content: str) -> bool:
    """原子写入 config.toml（复用 update_codex_config 的原子写入逻辑）"""
    try:
        config_dir = config_path.parent
        config_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".code-cn-bridge-config-",
            suffix=".tmp",
            dir=str(config_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        _logger.error("原子写入 config.toml 失败: %s", e)
        return False


def _remove_bridge_fields(content: str) -> str:
    """从 config.toml 内容中移除所有模型 provider 相关字段

    移除（用于切换到官方模式或清理旧配置）：
    - 顶层的 model_provider / model_catalog_json / model / model_reasoning_effort
    - 所有 [model_providers.*] 段及其子段（包括 custom / code-cn-bridge 等）

    保留 plugins/mcp_servers/projects/marketplaces 等其他配置不动。

    注意：Codex 桌面端运行时可能自己写入 model_provider = "custom" 和
    [model_providers.custom] 段，这些也需要清理，否则会导致 TOML 顶层
    重复声明（非法）。
    """
    lines = content.splitlines(keepends=True)
    new_lines: list[str] = []
    in_model_providers_section = False  # 是否在 [model_providers.*] 段内
    in_any_section = False

    for line in lines:
        stripped = line.strip()

        # 段检测
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            in_any_section = True
            # 检测是否进入 [model_providers.*] 段（包括子段如 [model_providers.xxx.model_info])
            if stripped.startswith("[model_providers."):
                in_model_providers_section = True
                continue  # 跳过段声明行
            else:
                in_model_providers_section = False
                new_lines.append(line)
                continue

        # 在 model_providers 段内：跳过所有内容
        if in_model_providers_section:
            continue

        # 顶层字段处理：移除所有模型相关顶层键
        if not in_any_section:
            # 移除所有 model_provider 声明（不管值是 custom 还是 code-cn-bridge）
            if stripped.startswith("model_provider") and "=" in stripped:
                continue
            # 移除桥接器的 catalog 声明
            if stripped.startswith("model_catalog_json") and "=" in stripped:
                continue
            # 移除 model 和 model_reasoning_effort（桥接器或 Codex 写入的默认值）
            if stripped.startswith("model ") or stripped.startswith("model_reasoning_effort"):
                continue

        new_lines.append(line)

    # 清理末尾多余空行
    result = "".join(new_lines)
    while "\n\n\n\n" in result:
        result = result.replace("\n\n\n\n", "\n\n\n")
    return result


def _add_bridge_fields(content: str, endpoint: str, default_model: str | None) -> str:
    """向 config.toml 添加桥接器配置字段

    添加：
    - 顶层：model_provider / model_catalog_json / model / model_reasoning_effort
    - 段：[model_providers.code-cn-bridge] 含 base_url / wire_api / requires_openai_auth

    采用段级增量编辑：保留其他所有段和字段不动。
    如果已存在 [model_providers.code-cn-bridge] 段，会替换其内容。
    """
    lines = content.splitlines(keepends=True)
    new_lines: list[str] = []
    inserted_top = False
    inserted_section = False
    in_section = False
    skipping_old_bridge_section = False  # 跳过已存在的桥接器段内容

    # 桥接器顶层配置块
    top_block_lines = [
        f'model_provider = "{BRIDGE_PROVIDER_NAME}"',
        f'model_catalog_json = "{DEFAULT_CATALOG_PATH.name}"',
    ]
    if default_model:
        top_block_lines.append(f'model = "{default_model}"')
        top_block_lines.append('model_reasoning_effort = "medium"')

    # 桥接器 provider 段
    section_block_lines = [
        f"[model_providers.{BRIDGE_PROVIDER_NAME}]",
        f'name = "Code CN Bridge"',
        f'base_url = "{endpoint}"',
        f'env_key = "OPENAI_API_KEY"',
        f'wire_api = "responses"',
        f'requires_openai_auth = false',
        f'supports_websockets = false',
    ]

    for line in lines:
        stripped = line.strip()

        # 段检测
        if stripped.startswith("[") and stripped.endswith("]") and not stripped.startswith("[["):
            # 在第一个段之前插入桥接器顶层配置（如果还没插入）
            if not inserted_top:
                for tl in top_block_lines:
                    new_lines.append(tl + "\n")
                new_lines.append("\n")  # 空行分隔
                inserted_top = True

            in_section = True

            # 检测是否是已存在的 [model_providers.*] 段
            # （包括 code-cn-bridge / custom / 其他旧 provider）
            # 跳过所有旧的 provider 段，由 _remove_bridge_fields 已清理
            # 但为了健壮性，这里也处理
            if stripped.startswith("[model_providers."):
                # 如果是第一次遇到 provider 段，插入新的 bridge provider 段
                if not inserted_section:
                    for sl in section_block_lines:
                        new_lines.append(sl + "\n")
                    inserted_section = True
                skipping_old_bridge_section = True
                continue  # 跳过原段声明
            else:
                # 遇到非 provider 段，停止跳过
                skipping_old_bridge_section = False
                new_lines.append(line)
                continue

        # 跳过已存在 provider 段内的旧内容
        if skipping_old_bridge_section:
            continue

        new_lines.append(line)

    # 如果遍历完没有段（纯顶层配置文件），在末尾追加
    if not inserted_top:
        for tl in top_block_lines:
            new_lines.append(tl + "\n")
        inserted_top = True

    # 如果没有插入 provider 段（原文件没有该段），在末尾追加
    if not inserted_section:
        new_lines.append("\n")
        for sl in section_block_lines:
            new_lines.append(sl + "\n")
        inserted_section = True

    return "".join(new_lines)


def switch_codex_mode(mode: str) -> dict:
    """切换 Codex 配置模式（参考 CC Switch 的配置切换器）

    模式说明：
    - "official"：使用官方 ChatGPT 账号，移除所有桥接器字段
      保留 plugins/mcp_servers/projects 等其他配置不动
    - "bridge"：使用桥接器代理，添加 model_provider/base_url/catalog 等
      保留官方登录态（auth.json 不动），插件功能继续可用

    采用段级增量编辑 + 原子写入，不覆盖整个文件。

    Args:
        mode: "official" 或 "bridge"

    Returns:
        dict: {success, mode, message, ...详情}
    """
    config_path = CODEX_HOME / "config.toml"

    # 确保 ~/.codex 目录存在
    CODEX_HOME.mkdir(parents=True, exist_ok=True)

    # config.toml 不存在时创建最小配置（新用户无需手动安装 Codex 也能用）
    if not config_path.exists():
        config_path.write_text(
            '# Codex 配置 - 由 Code CN Bridge 自动创建\n'
            'service_tier = "default"\n\n'
            '[features]\n',
            encoding="utf-8",
        )
        _logger.info("自动创建 config.toml: %s", config_path)

    try:
        content = config_path.read_text(encoding="utf-8")
    except Exception as e:
        return {
            "success": False,
            "mode": mode,
            "message": f"读取 config.toml 失败: {e}",
        }

    if mode == "official":
        new_content = _remove_bridge_fields(content)
        msg = "已切换到官方模式：移除桥接器 provider/catalog/model 字段"

    elif mode == "bridge":
        # auth.json 不存在时自动生成最小占位文件（免 ChatGPT 登录）
        # 仅当 auth.json 不存在时才创建，已有官方登录态则保留
        auth_path = CODEX_HOME / "auth.json"
        if not auth_path.exists():
            import json
            auth_path.write_text(
                json.dumps({"OPENAI_API_KEY": "sk-bridge-local"}, indent=2),
                encoding="utf-8",
            )
            _logger.info("自动创建占位 auth.json（免登录）: %s", auth_path)

        # 生成 catalog 文件
        try:
            catalog_path = generate_catalog()
        except Exception as e:
            return {
                "success": False,
                "mode": mode,
                "message": f"生成 catalog 失败: {e}",
            }

        # 收集配置信息
        cfg = get_config()
        host = cfg.server_host
        port = cfg.server_port
        endpoint = f"http://{host}:{port}/v1"
        enabled_models = _collect_enabled_models(cfg)
        default_model = _pick_default_model(enabled_models)

        if not enabled_models:
            return {
                "success": False,
                "mode": mode,
                "message": "桥接器未配置任何启用的模型，请先在'模型'页面添加",
            }

        # 先移除旧的桥接器字段（避免重复），再添加新的
        cleaned = _remove_bridge_fields(content)
        new_content = _add_bridge_fields(cleaned, endpoint, default_model)
        msg = f"已切换到桥接器模式：provider={BRIDGE_PROVIDER_NAME}, model={default_model}, endpoint={endpoint}"

    else:
        return {
            "success": False,
            "mode": mode,
            "message": f"未知模式: {mode}（支持: official / bridge）",
        }

    # 原子写入
    if not _atomic_write_config(config_path, new_content):
        return {
            "success": False,
            "mode": mode,
            "message": "原子写入 config.toml 失败",
        }

    _logger.info("Codex 模式切换: %s", msg)
    return {
        "success": True,
        "mode": mode,
        "message": msg,
        "endpoint": f"http://{cfg.server_host}:{cfg.server_port}/v1" if mode == "bridge" else None,
        "default_model": default_model if mode == "bridge" else None,
    }


if __name__ == "__main__":
    # 直接运行时生成 catalog 并更新 config
    path = generate_catalog()
    print(f"已生成 catalog: {path}")
    if update_codex_config(path):
        print("已更新 config.toml")
    else:
        print("更新 config.toml 失败")
