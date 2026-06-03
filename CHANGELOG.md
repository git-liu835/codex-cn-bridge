# Changelog

## v1.0.0 (2026-06-04) — 稳定性与长对话增强

### 新功能

- **熔断保护**: 新增 provider 级熔断器（3 次连续失败→断开 30 秒→半开探测），上游 API 异常时快速返回 503 而非长时间挂起。
- **多 Key 轮换**: 每个 provider 支持配置多个 API Key（`api_keys` 列表或 `api_keys_env` 逗号分隔），流式重试时自动切换到下一个 Key，避免单 Key 限流导致对话中断。
- **推理内容缓存恢复**: DeepSeek 在 tool_calls 消息中偶发丢失 `reasoning_content`，自动从缓存注入恢复，确保后续对话能正确引用前轮推理。
- **会话粘性**: 同一 `conversation.id` 在 10 分钟内始终路由到同一 provider，避免多 provider 场景下上下文断裂。
- **响应预热**: 流式请求发起前立即下发 `response.created` 事件，减少用户感知的首字节延迟。
- **工具调用延迟加载**: SSE 工具调用事件推迟到首个参数到达才发送，避免 Codex 收到空工具调用而卡住。
- **响应缓存磁盘持久化**: 对话摘要缓存落地到 `~/.code-cn-bridge/cache/responses/`（原子写入+重命名），Bridge 重启后缓存不丢失。
- **项目上下文自动注入**: 新增 `project_context` 配置项，自动检测项目根目录的 CODEX.md/CLAUDE.md/.cursorrules 等规则文件并注入到系统指令，模型无需每次重新了解项目结构。
- **Codex 历史感知**: 支持读取 Codex 桌面端的 `state_5.sqlite` 数据库，将最近对话摘要作为上下文注入，实现跨会话记忆。
- **空洞响应自动重试**: 检测模型返回的无实质内容响应（无工具调用且文本不足 50 字符），自动关闭思考模式重试一次。
- **Thinking Budget 自动修正**: 检测 DeepSeek `budget_tokens` 参数错误，自动修正参数后重试。

### 改进

- 所有适配器新增 `supports_thinking_budget` 标志位，DeepSeek 支持，其他模型自动省略 budget_tokens 参数。
- 非 DeepSeek 适配器（Kimi/GLM/Qwen/Doubao）新增 max_tokens 下限保护（16384），防止 thinking 耗尽所有输出 token。
- 流式心跳间隔优化为 25 秒，读超时提升至 600 秒，适应长时间推理场景。
- 流式重试时自动轮换 API Key + 重建翻译器，避免状态污染。
- `httpx` 客户端全局设置 `trust_env=False`，避免系统代理（Clash/V2Ray）导致 TLS 握手间歇性失败。
- SSE 事件完整性修复：工具调用和文本输出项均正确发送 `done` 事件。
- 日志新增详细模式（`DetailedLoggingMiddleware`），桌面端可查看请求/响应体（已脱敏）。

## v0.3.17 (2026-05-26)

### Features

- **推理模式 (Reasoning)**: 启用 DeepSeek/Kimi/GLM 的 thinking 模式，不再强制禁用。模型的 `reasoning_content` 被正确转换为 Responses API 的 `reasoning` 输出项，Codex 能完整看到模型的思考过程，大幅提升复杂任务和工具调用的准确性。
- **per-model 推理开关**: `model_mapping` 新增 `enable_thinking` 字段，可按模型单独控制是否启用推理模式。

### Improvements

- **流式自动重试**: 上游连接断开时自动重试一次，防止长时间推理后断连导致对话上下文丢失。
- **心跳优化**: 流式心跳间隔从 15s 优化到 10s，更快检测断连。
- **注册表自修复**: Electron 启动时自动维护 Windows 注册表中的 `InstallLocation`，确保自动更新始终找到正确的安装路径。

## v0.3.16 (2026-05-26)

### Bug Fixes

- **别名重命名不生效**: 编辑模型卡片时修改 alias 字段，后端始终用 URL 路径的旧键名保存，导致重命名无法持久化。现在 `update_model` 支持三种重命名场景：简单重命名、合并到已有别名、从多模型列表提取单个条目。
- **adapter 字段被忽略**: `update_model` 未处理 `adapter` 字段，编辑卡片时修改适配器不生效。
- **CORS 预检失败**: Electron 生产模式使用 `file://` 来源，CORS 允许列表缺失导致 PUT 请求预检返回 400。
- **日志重复**: package logger 和 root logger 都往 `bridge.log` 写入，导致每条日志出现两次。

## v0.3.15 (2026-05-26)

### Features

- **多模型切换**: 同一个 code 模型别名（如 `gpt-5.5`）可映射到多个后端模型，桌面端支持一键切换

## v0.3.14 (2026-05-26)

### Bug Fixes

- **桌面图标**: 移除 BrowserWindow 的 `createEmpty()` 空白图标覆盖，修复安装后显示原生 Electron logo

## v0.3.13 (2026-05-26)

### Bug Fixes

- **撤销身份注入**: 修改 system prompt 导致所有请求空输出，已回退

## v0.3.12 (2026-05-26)

### Bug Fixes

- **SHA512 再次修复**: release job 直接重新计算安装包 SHA512，不再依赖 electron-builder 记录的旧 hash，消除 artifact 传输过程造成的校验差异

## v0.3.11 (2026-05-26)

### Bug Fixes

- **模型身份注入空输出**: 身份信息合并到 instructions 而非单独 system 消息，软化措辞避免触发安全过滤

## v0.3.10 (2026-05-26)

### Bug Fixes

- **SHA512 校验失败**: 修复 CI 多平台并行构建时 latest.yml 互相覆盖，导致校验和不匹配

## v0.3.9 (2026-05-26)

### Features

- **模型身份注入**: 代理请求自动注入 system 消息，模型问到时以配置的目标模型名自居，不再瞎编身份

## v0.3.8 (2026-05-26)

### Bug Fixes

- **安装包下载 404**: 添加显式 `artifactName` 模板，修复 `productName` 含空格导致 `latest.yml` 文件名与实际发布文件不匹配的问题

## v0.3.7 (2026-05-26)

### Features

- **更新镜像支持**: About 页面新增镜像 URL 配置，解决国内访问 GitHub 超时问题。通过镜像 API 获取最新 release 信息，再用 generic provider 下载安装包

## v0.3.6 (2026-05-25)

### Bug Fixes

- **更新检查 404**: 修复 electron-builder repo 名错误 (code-cn-bridge → codex-cn-bridge)，导致 latest.yml 404
- **latest.yml 未上传**: CI workflow 添加 `*.yml` 到构建产物上传列表
- **配置文件损坏**: 改用原子写入（temp file + rename），防止更新时杀进程导致配置丢失
- **更新日志可复制**: About 页面报错信息用 code 块 + 复制按钮展示

### Commits

- `6d82b67` — fix: auto-update repo name and missing latest.yml
- `100c9db` — fix: atomic config write, copyable error logs

## v0.3.5 (2026-05-25)

### Features

- **详细日志开关**: 监控日志页面新增开关，开启后捕获每条 /v1/ 请求的完整请求体和响应体，点击可展开查看 JSON 内容，自动保留最近 100 条
- **一键安装命令**: README 添加各平台命令行一键安装命令（Windows/macOS/Linux）

### Bug Fixes

- **模型卡片被覆盖**: 修复在已有 provider 下添加新模型时，空表单字段覆盖 provider 的 base_url/api_key_env 配置
- **update_model 重复代码**: 删除 update_model 端点中重复执行的 advanced 代码块
- **卡片表单重名检查**: handleAddCard 添加 alias 重名检查，防止覆盖已有模型配置

### Commits

- `13f8e0d` — feat: detailed request logging toggle + fix model card overwrite bug

## v0.3.4 (2026-05-23)

### Features

- **自动更新**: 启动时自动检查 GitHub Releases 更新，弹窗提示用户选择是否下载更新
- **手动检查更新**: About 页面添加「检查更新」按钮，可随时检查新版本
- **更新流程用户可控**: 每步均由用户决定（立即更新/稍后提醒 → 退出并安装/稍后安装），不自动下载

### Improvements

- **About 页面**: 版本号动态获取，修复 GitHub 链接为正确仓库地址

### Commits

- `6cb9745` — feat: auto-update with user-choice dialogs, bump to 0.3.4

## v0.3.3 (2026-05-23)

### Bug Fixes

- **托盘图标空白**: 托盘图标从空白透明图改为加载 `assets/icon.png` 真实图标，按平台缩放（Win 16px, Mac 44px）
- **多个托盘图标残留**: 添加 `app.requestSingleInstanceLock()` 单实例锁，重复启动时激活已有窗口；退出时 `tray.destroy()` 清理托盘
- **electron-builder**: 添加 `icon.png` 到 `extraResources`，确保生产构建包含图标文件

### Commits

- `a31c4f4` — fix: prevent multiple blank tray icons on Windows
- `77376f8` — fix: prevent dangling tool_calls causing upstream 400 error

## v0.3.2 (2026-05-23)

### Bug Fixes

- **工具调用报 400 错误**: 修复 `protocol.py` 中 `_map_input_to_messages()` 的 `_flush_tool_calls()` 逻辑。当某些 `function_call` 项缺少对应的 `function_call_output` 响应时（多轮工具调用、历史截断等场景），不再创建尾部没有 `tool` message 跟随的 `assistant` 消息，避免上游 API（DeepSeek 等）返回 `"An assistant message with 'tool_calls' must be followed by tool messages"` 错误

### Commits

- `77376f8` — fix: prevent dangling tool_calls causing upstream 400 error

## v0.3.1 (2026-05-23)

### Bug Fixes

- **模型配置重启丢失**: 修复 `config.py` 中首次启动无配置文件时 `_config_path` 为 `None` 导致 `save()` 静默跳过的问题。现在 `_config_path` 默认指向 `~/.code-cn-bridge.yaml`，首次保存时自动创建

### Commits

- `7bbdb19` — fix: persist model configs when no config file exists on first launch
