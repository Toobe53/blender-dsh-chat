# Changelog

## 1.0.0 — 2026-11-10

首个正式版。

- 内嵌对话面板（3D 视图 → Item 页），流式输出与工具调用回显
- 模型选择（DeepSeek-V4.1-Flash / V4-Pro）与思考深度（Off/Low/High/Max）
- 权限确认：允许一次 / 拒绝 / 本会话总是允许
- 会话列表与续接（保留 agent 上下文，自动带原 cwd）
- 插件管理：列出 profile bundles、开关、安装本地 bundle
- 账户余额查询与本次会话余额差值估算
- 依赖自检（dsh / node / python / MCP 插件 / 端口 / profile）
- 可移植：偏好设置、内置 MCP bridge、ACP profile 自举
- 使用 Blender 自带 Python，移除系统 python3 依赖

## 0.6.0

- 账户余额（`/user/balance`）与每轮会话费用估算

## 0.5.0

- 插件列表 / 开关 / 本地 bundle 安装

## 0.4.0

- 会话列表与按会话续接

## 0.3.0

- 沙箱升级权限确认
- 偏好设置、内置 bridge、profile 自举

## 0.2.0

- 模型与思考深度切换
- `续接上次`

## 0.1.0

- 首个可运行版本：ACP 客户端、面板、流式对话、Blender MCP 工具
