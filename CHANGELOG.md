# Changelog

## [0.6.2] - 2026-07-10

### Fixed
- 桥接器模式切换后 Codex 仍报 `Missing environment variable: OPENAI_API_KEY`
  - `config.toml` 改用 `experimental_bearer_token`，不再依赖系统环境变量
  - 切换时自动补全残缺 `auth.json` 占位 Key
  - Windows 下自动写入用户级 `OPENAI_API_KEY`（兼容旧版 Codex）
