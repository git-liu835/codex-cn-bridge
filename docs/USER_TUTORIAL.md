# Code CN Bridge 使用教程

> 安装代理 → 配置模型 → Claude Code 接入国产大模型，三步搞定。

---

## 一、下载安装

从 [GitHub Releases](https://github.com/git-liu835/code-cn-bridge/releases) 下载对应平台的安装包：

| 平台 | 安装包 |
|------|--------|
| Windows | `code-CN-Bridge-Setup-0.1.0.exe` |
| macOS | `code-CN-Bridge-0.1.0.dmg` |
| Linux | `code-CN-Bridge-0.1.0.AppImage` |

- **Windows**：双击 exe 安装，勾选"创建桌面快捷方式"。
- **macOS**：双击 dmg，拖入 Applications。首次打开需在「系统设置 → 隐私与安全性」中允许。
- **Linux**：`chmod +x code-CN-Bridge-0.1.0.AppImage` 然后双击运行。

安装后双击桌面图标启动应用。

![启动后的仪表板界面](assets/dashboard.png)

> 关闭主窗口后代理继续在系统托盘运行，不会退出。右键托盘图标可彻底退出。

---

## 二、获取 API Key

用哪个厂商的模型就去哪家领 Key，选一个即可：

| 厂商 | 申请地址 | Key 示例 |
|------|----------|----------|
| **DeepSeek** | [platform.deepseek.com](https://platform.deepseek.com/) | `sk-xxxxxxxx` |
| **通义千问** | [bailian.console.aliyun.com](https://bailian.console.aliyun.com/) | `sk-xxxxxxxx` |
| **Kimi** | [platform.moonshot.cn](https://platform.moonshot.cn/) | `sk-xxxxxxxx` |
| **豆包/火山引擎** | [console.volcengine.com/ark](https://console.volcengine.com/ark/) | 申请后可见 |

> 注册 → 进入 API 密钥管理 → 点击创建 → 复制保存。**Key 只显示一次，记得存好。**

![获取 API Key 示意图](assets/apikey.png)

---

## 三、配置代理

回到桌面应用，进入 **「模型配置」** 页面。

### 第 1 步：添加 Provider

点击 **「+ 添加 Provider」**，填写：

| 字段 | 值 |
|------|-----|
| Provider 名称 | 随意，比如 `deepseek` |
| 适配器 | 选对应的（deepseek / qwen / kimi / doubao） |
| Base URL | 自动填入，一般不用改 |
| API Key | 粘贴你刚复制的 Key |
| API Key 环境变量名 | 用 Key 值方式就不用填这个 |

点击「保存」。

![添加 Provider](assets/add-provider.png)

### 第 2 步：添加模型映射

点击 **「+ 添加模型映射」**，填写：

| 字段 | 值 |
|------|-----|
| 别名 | `gpt-5-code` |
| 目标模型 | 实际模型名，如 `deepseek-v4-pro` |
| Provider | 选上一步创建的 |
| 模型类型 | 文本 |

点击「保存」。

![添加模型映射](assets/add-model.png)

也可以再加一条便宜模型用于简单任务：

| 别名 | 目标模型 | Provider |
|------|----------|----------|
| `gpt-5-code-light` | `deepseek-v4-pro` | deepseek |

---

## 四、启动代理

在仪表板点击 **「启动代理」**，状态变绿就 ok 了。

![代理运行中](assets/running.png)

---

## 五、配置 Claude Code

在终端中设置两个环境变量：

**Windows（PowerShell）：**
```powershell
$env:OPENAI_BASE_URL="http://localhost:8765/v1"
$env:OPENAI_API_KEY="any-value"
```

**macOS / Linux：**
```bash
export OPENAI_BASE_URL="http://localhost:8765/v1"
export OPENAI_API_KEY="any-value"
```

> 也可以写入配置文件永久生效：Linux/Mac 写 `~/.zshrc`；Windows 在系统环境变量里添加。

然后正常使用 Claude Code 即可：

```bash
claude
```

代理的 **监控日志** 页面会实时显示请求记录，方便排查问题。

![监控日志](assets/logs.png)

---

## 六、桌面应用页面速览

| 页面 | 干什么用 |
|------|----------|
| **仪表板** | 看代理状态、请求统计、模型健康 |
| **模型配置** | 管 Provider 和模型映射，测连通性 |
| **全局设置** | 改端口、日志级别、界面主题/语言、导入导出配置 |
| **监控日志** | 实时看每条请求的状态码、耗时、目标模型 |
| **关于** | 版本号、项目链接、QQ 群 |

---

## 七、常见问题

**Q: 怎么确认是 Key 问题还是桥接器问题？**

先直连 DeepSeek（方法 A），再走本地桥接器（方法 B）：

```powershell
# 方法 A：直连官方（验证 Key）
$headers = @{ Authorization = "Bearer $env:DEEPSEEK_API_KEY"; "Content-Type" = "application/json" }
$body = '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
Invoke-WebRequest -Uri "https://api.deepseek.com/v1/chat/completions" -Method POST -Headers $headers -Body $body

# 方法 B：走桥接器（验证代理）—— 先启动 Bridge
$headers = @{ Authorization = "Bearer any-value"; "Content-Type" = "application/json" }
Invoke-WebRequest -Uri "http://localhost:8765/v1/chat/completions" -Method POST -Headers $headers -Body $body
```

- A 失败 → Key / 余额 / 网络问题  
- A 通、B 失败 → 桥接器配置问题（看 `~/.code-cn-bridge.yaml` 的 provider、映射、是否启动）

**Q: Claude Code / Codex 连不上？**

```bash
# 确认代理在运行
curl http://localhost:8765/health
# 确认环境变量
echo $OPENAI_BASE_URL
```

**Q: 报 400 错误？**

多半是图片问题——当前文本模型不支持图片，但你的对话里带了图。勾选「视觉路由」即可解决（设置页 → 开启视觉路由 → 选一个视觉模型）。

**Q: 关窗口后怎么找回来？**

在 Windows 右下角系统托盘 / macOS 顶部菜单栏找到图标，右键「显示主窗口」。

---

## 快速检查清单

- [ ] 安装包已下载安装
- [ ] 拿到至少一个厂商的 API Key
- [ ] 在桌面应用中添加了 Provider 和模型映射
- [ ] 代理显示 🟢 运行中
- [ ] 终端设置了 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`
- [ ] `claude` 能正常对话

---

> **交流反馈：** QQ 群 `1095150579`
> **GitHub：** [git-liu835/code-cn-bridge](https://github.com/git-liu835/code-cn-bridge)
