# DSH Chat for Blender

在 Blender 里和 DeepSeek Harness 对话，agent 的操作实时落在你当前打开的场景里。

[GitHub](https://github.com/Toobe53/blender-dsh-chat) · MIT · v1.0.0

![面板](panel.png)

## 特性

| | |
|---|---|
| **内嵌对话** | 3D 视图 → 侧栏 **Item** 页；流式输出、工具调用逐条显示 |
| **模型 / 思考** | DeepSeek-V4.1-Flash / V4-Pro；Off / Low / High / Max |
| **权限确认** | 沙箱升级时：允许一次 / 拒绝 / 本会话总是允许 |
| **会话** | 最近 15 个会话，按原工作目录续接 |
| **插件** | 列出 profile bundles、开关、安装本地 bundle |
| **余额** | 账户余额 + 本次会话差值估算 |
| **自检** | 一键检查 dsh / node / python / MCP 插件 / 端口 / profile |
| **可移植** | 偏好设置、内置 MCP bridge、ACP profile 自举；用 Blender 自带 Python |

## 安装

1. 下载 release 的 `blender_dsh_chat-1.0.0.zip`；
2. 偏好设置 → 插件 → **安装…** → 选 zip → 勾选 **DSH Chat**；
3. 3D 视图按 `N` → **Item** 页 → 「DSH 对话」→ 输入 → 发送。

依赖（详见 [DEPENDENCIES.md](DEPENDENCIES.md)）：

- Blender 5.1+（含官方 MCP 插件，需启用，端口 9876）
- DeepSeek Harness `dsh` + Node.js 20+
- DeepSeek 账户（`deepseek-account`）
- 无需系统 Python（用 Blender 自带的）

## 文档

- [正式介绍](docs/guide.zh.md) —— 原理、安装、使用、安全边界
- [依赖清单](DEPENDENCIES.md)
- [更新日志](CHANGELOG.md)

## 原理

```
Blender 面板 ── ACP (stdio) ── dsh --profile blender ── agent
                                                │ MCP
                                                ▼
                    官方 MCP 插件 (:9876) ── 当前场景
```

## 已知限制

- 单行输入；对话历史不持久化。
- 权限确认只在沙箱升级时触发，不是逐工具。
- 费用是余额差值估算。
- 主要在 macOS 验证；Linux/Windows 预期可用但未系统测试。

## 许可

MIT
