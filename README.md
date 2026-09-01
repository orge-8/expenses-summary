# expenses-summary（今日模型花费总结）

统计当天模型调用次数、token 消耗、回复量和回复成本，生成财报图片并合并转发到聊天。

参考实现：[DavidBlackCN/maibot-expenses-summary-plugin](https://github.com/DavidBlackCN/maibot-expenses-summary-plugin)，按本机 `maibot-plugin-sdk 2.8.0` 的真实 API 签名重写。

## 功能

- 统计今日模型请求、回复消息量、回复成本、token 消耗（本地日期 0 点至当前，小时粒度过滤，避免跨天重复计入）
- 财报图片渲染（`render.html2png`），含累计请求 / 回复 / 成本 / Token 指标卡与各模型成本排行（附各模型 token 数）
- `/expenses`、`/今日财报` 即时查询
- `expenses_summary` 工具可被麦麦 LLM 主动调用
- 合并转发发送（失败自动退回逐条普通消息）
- 可选每日定时推送到指定群/私聊

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/expenses`、`/今日财报` | 所有人（可配置仅管理员） | 立即生成并发送财报 |

## 配置（`config.toml`，Runner 首次加载自动生成）

```toml
[plugin]
config_version = "1"
enabled = true

[report]
title = "今日模型调用财报"
use_forward_message = true      # false 则逐条普通消息发送
opening = "{date}模型调用财报已生成，……"   # {date} 为占位符

[permission]
query_admin_only = false        # true 时查询命令仅管理员可用
admins = []                     # 管理员 QQ 号列表

[scheduler]
enabled = false                 # 启用每日定时推送
time = "23:30"
group_ids = []                  # 推送目标群号
private_ids = []                # 推送目标私聊 QQ
```

注意：WebUI 改配置不会推送给运行中的插件，改完需完整重启 MaiBot。

## 数据来源

- `statistics.local.model_trend`（metric=cost / request，bucket=hour，days=1）
- `statistics.local.token_trend`（group_by=model，bucket=hour，days=1；失败自动退回整体趋势，仍失败则本次财报不含 token）
- `statistics.local.message_trend`（bucket=hour，days=1）
- 小时桶按本地日期过滤，只累计「今天 0 点之后」的数据

## 安装

把整个 `expenses-summary` 目录放进 MaiBot 的 `plugins/` 目录，确认 `_manifest.json` 与 `plugin.py` 同级，重启 MaiBot。

## 兼容性

- MaiBot ≥ 1.0.9，SDK ≥ 2.6.0
- 声明能力：`send.text` / `send.image` / `send.forward` / `chat.*`（定时解析会话）/ `render.html2png` / `statistics.local.*`（含 `token_trend`）

## 常见问题

- **命令没反应**：先确认 WebUI「Bot 配置 → 命令」里组件已注册（新命令可能需重启）；再查日志有无 `E_CAPABILITY_DENIED`（capabilities 改动后必须重启）。
- **财报图生成失败**：插件会自动退回纯文本表格发送，不影响数据展示。
- **定时没发**：确认 `scheduler.enabled=true` 且目标列表非空；日志会打「定时财报 -> 群 x: 成功/失败」。
