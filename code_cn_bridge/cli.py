"""CLI 入口 —— 提供 code-cn-bridge 命令行工具"""

from __future__ import annotations

import sys
from pathlib import Path

import click


@click.group()
@click.version_option(version="0.3.20", prog_name="code-cn-bridge")
def main():
    """code CN Bridge - 国产模型本地代理

    将 OpenAI Responses API 转换为 Chat Completions API，
    使 code CLI 无缝接入通义千问、DeepSeek、Kimi 等模型。
    """


@main.command()
@click.option(
    "-c", "--config",
    default=None,
    type=click.Path(exists=False),
    help="配置文件路径（默认: ~/.code-cn-bridge.yaml）",
)
@click.option(
    "-p", "--port",
    default=None,
    type=int,
    help="服务端口（默认: 8765）",
)
@click.option(
    "-h", "--host",
    default=None,
    help="服务监听地址（默认: 127.0.0.1）",
)
@click.option(
    "-v", "--verbose",
    is_flag=True,
    help="开启详细日志输出（调试用）",
)
def start(config: str | None, port: int | None, host: str | None, verbose: bool):
    """启动 code CN Bridge 代理服务"""
    import uvicorn
    import os

    # 设置配置路径
    if config:
        os.environ["CODE_CN_BRIDGE_CONFIG"] = config

    # 导入配置（触发加载）
    from .config import get_config
    cfg = get_config(config)

    bind_host = host or cfg.server_host
    bind_port = port or cfg.server_port

    click.echo(f"code CN Bridge v0.3.20")
    click.echo(f"服务地址: http://{bind_host}:{bind_port}")
    click.echo(f"配置文件: {cfg._config_path or '默认'}")

    if verbose:
        from .server import _setup_logging
        _setup_logging(verbose=True)

    uvicorn.run(
        "code_cn_bridge.server:create_app",
        host=bind_host,
        port=bind_port,
        log_level="debug" if verbose else "info",
        factory=True,
    )


@main.command()
@click.option(
    "--provider",
    default="qwen",
    type=click.Choice(["qwen", "deepseek", "kimi"]),
    help="选择默认模型提供商",
)
@click.option(
    "--api-key",
    default=None,
    help="设置 API Key",
)
@click.option(
    "-o", "--output",
    default=None,
    type=click.Path(),
    help="配置文件输出路径（默认: ~/.code-cn-bridge.yaml）",
)
def init(provider: str, api_key: str | None, output: str | None):
    """初始化配置文件"""
    from .config import Config

    output_path = Path(output) if output else Path.home() / ".code-cn-bridge.yaml"

    if output_path.exists():
        if not click.confirm(f"配置文件 {output_path} 已存在，是否覆盖？"):
            click.echo("已取消")
            return

    Config.generate_default(output_path)

    click.echo(f"配置文件已生成: {output_path}")
    click.echo()
    click.echo("下一步:")
    click.echo(f"  1. 设置环境变量: export {provider.upper()}_API_KEY=<你的API Key>")
    click.echo("  2. 启动代理: code-cn-bridge start")
    click.echo("  3. 配置 code CLI:")
    click.echo('     export OPENAI_BASE_URL="http://localhost:8765/v1"')
    click.echo('     export OPENAI_API_KEY="any-value"')


@main.command()
def list_adapters():
    """列出所有可用的适配器"""
    from .adapters import get_registry
    reg = get_registry()
    click.echo("可用适配器:")
    for name in reg.list():
        a = reg.get(name)
        click.echo(f"  {name:12s} → {a.base_url}")


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
def validate(config_path: str):
    """验证配置文件"""
    from .config import Config

    try:
        cfg = Config(config_path)
        click.echo(f"✓ 配置文件有效: {config_path}")
        click.echo(f"  服务: {cfg.server_host}:{cfg.server_port}")
        click.echo(f"  Provider: {list(cfg.providers.keys())}")
        click.echo(f"  模型映射: {cfg.model_mapping}")
    except Exception as exc:
        click.echo(f"✗ 配置文件无效: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
