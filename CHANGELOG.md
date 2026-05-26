# Changelog

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
