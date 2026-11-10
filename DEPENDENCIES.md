# 依赖清单

DSH Chat 自身只用 Python 标准库；运行时依赖集中在“宿主”和“DSH agent”两侧。

## 必需

| 依赖 | 最低版本 | 本机验证 | 检查方式 | 说明 |
|---|---|---|---|---|
| Blender | 5.1 | 5.2.3 LTS | `blender --version` | 需要官方 MCP 插件（5.1 起随 Blender 提供） |
| DeepSeek Harness (`dsh`) | 0.2.x | 0.2.2-rc.1 | `which dsh` / `dsh --version` | ACP 服务端 |
| Node.js | 20 | 24.13.1 | `node --version` | `dsh` 的运行时，`dsh` 通过 `#!/usr/bin/env node` 启动 |
| DeepSeek 账户 | — | 已登录 | DSH 网页版账户页 | 使用 `deepseek-account` provider |
| 官方 MCP 插件 | 1.0.3 | 1.0.3 | 偏好设置 → 扩展 → `MCP` | 监听 `127.0.0.1:9876` |

## 内置，无需安装

| 依赖 | 说明 |
|---|---|
| Python | 直接使用 Blender 自带的解释器（`sys.executable`），不依赖系统 `python3` |
| `blender_mcp_bridge.py` | 随插件打包，负责 MCP(stdio) ↔ Blender socket 的翻译 |
| ACP profile | 首次连接自动创建 `~/.dsh/profiles/blender/package.json` |

## 可选

| 依赖 | 用途 | 缺失影响 |
|---|---|---|
| pnpm | 「安装本地 Bundle…」 | 手动用 `dsh plugin --profile blender add ...` 代替 |
| `dsh-context` 等 agent bundle | 给 agent 增加工具/技能 | 不影响对话与 Blender 操作 |

## 一键检查

Blender → 偏好设置 → 插件 → **DSH Chat** → **检查依赖**：

```
✓ dsh: /usr/local/bin/dsh
✓ node: /usr/local/bin/node
✓ python: .../Blender.app/Contents/Resources/5.2/python/bin/python3.13
✓ MCP bridge: .../blender_dsh_chat/blender_mcp_bridge.py
✓ ACP profile: ~/.dsh/profiles/blender/package.json
✓ MCP add-on: 已启用
✓ port 9876: 监听中
```

任何一项 ✗，面板和偏好设置里都会给出修复提示。
