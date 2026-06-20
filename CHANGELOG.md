# Changelog

## v0.4.1

### 新增
- 新增「Codex 配置助手」：桌面端「全局设置」页一键生成 `config.toml` 和 `auth.json`，解决 Codex 桌面版/CLI 启动白屏、加载慢、Reconnecting 问题。
- 新增 `/admin/api/codex-auth` 端点：返回免登录占位 `auth.json` 内容。
- 增强生成的 `config.toml`：包含 `requires_openai_auth = false`（跳过 OAuth 登录窗口）、`supports_websockets = false`（解决 Reconnecting 卡顿）、每个启用模型的 `model_info` 能力声明表。

### 变更
- 桌面端 `Settings.tsx` 新增 Codex 配置助手卡片，支持一键生成、复制、下载 `config.toml` 与 `auth.json`。
- 桌面端 `api.ts` 新增 `getCodexConfig` 和 `getCodexAuth` 方法。
- `.gitignore` 新增 `.trae/` 和临时验证脚本过滤，避免推送开发内部文档。

### 修复
- 解决 Codex 桌面应用启动白屏：通过 `requires_openai_auth = false` 跳过访问 `auth.openai.com` 的 OAuth 登录窗口。
- 解决 Codex 启动后卡在 Reconnecting：通过 `supports_websockets = false` 禁用 WebSocket 连接。
- 解决无 `auth.json` 时 Codex 强制弹登录窗口的问题。

### 隐私与安全
- 更新日志仅包含功能与配置变更，不包含任何 API 密钥、个人凭据或私有部署地址。

## v0.4.0

### 新增
- 新增 Agnes AI 平台适配，支持文本对话、图像生成与视频生成模型。
- 新增模型映射：
  - 文本：`agnes-2.0-flash`、`agnes-1.5-flash`
  - 图像：`agnes-image-2.1-flash`、`agnes-image-2.0-flash`
  - 视频：`agnes-video-v2.0`
- 新增 `code_cn_bridge/adapters/agnes.py` 适配器，兼容 OpenAI V1 协议。

### 变更
- 桌面端与后端版本号同步提升至 `0.4.0`。
- 构建脚本 `scripts/build-backend.py` 增加 `--user` 参数，避免非管理员环境下 pip 安装权限不足。

### 隐私与安全
- 更新日志仅包含功能与配置变更，不包含任何 API 密钥、个人凭据或私有部署地址。
