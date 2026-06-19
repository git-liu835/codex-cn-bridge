# Changelog

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
