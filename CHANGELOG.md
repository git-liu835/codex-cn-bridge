# Changelog

## [0.6.3] - 2026-07-11

### Changed
- DeepSeek 仅保留 V4 系列：`deepseek-v4-pro` / `deepseek-v4-flash`（移除即将下线的 chat/reasoner）
- DeepSeek 上下文按官方 1M 配置；接近 85% 自动压缩，95% 硬截断
- 厂商预设统一到 `provider_presets.py`，每家一份模型列表并校验 base_url
- 添加厂商卡片时一次性写入整家模型列表，Codex 下拉可切换同厂商模型

### Fixed
- 请求 `deepseek-chat` 等原生模型名时不再被改写成默认模型
- DeepSeek thinking 参数对齐官方 `thinking` + `reasoning_effort`
- 移除会触发 DeepSeek/GLM 空输出的 instructions 中文注入

## [0.6.2] - 2026-07-10

### Fixed
- 桥接器模式切换后 Codex 仍报 `Missing environment variable: OPENAI_API_KEY`
  - `config.toml` 改用 `experimental_bearer_token`，不再依赖系统环境变量
  - 切换时自动补全残缺 `auth.json` 占位 Key
  - Windows 下自动写入用户级 `OPENAI_API_KEY`（兼容旧版 Codex）
