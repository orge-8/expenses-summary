"""今日模型花费总结插件

工作方式:
1. 通过 self.ctx.statistics.local 读取本机统计:
   - model_trend(metric="cost" / "request", bucket="hour") 取小时粒度趋势,
     按本地日期过滤「今天 0 点之后」的桶, 避免新的一天继续计入前一日 24H 数据;
   - token_trend(group_by="model") 统计今日 token 消耗;
   - message_trend 同理, 统计今日回复消息量。
2. 用 self.ctx.render.html2png 把财报渲染成图片。
3. 默认用 send.forward 合并转发「开头语 + 财报图」; 可配置为逐条普通消息发送。
4. /expenses、/今日财报 即时查询; expenses_summary 工具供 LLM 主动调用;
   支持每天定时推送到指定群/私聊。

统计口径: days=1 拉最近 24H 小时桶, 再按本地日期截断, 是「今日 0 点至当前」的准确口径。
"""

import asyncio
import base64
import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

# ---------- 纯函数区（不依赖 maibot_sdk, 可独立测试） ----------


@dataclass
class ModelCost:
    """单个模型的当日统计。"""

    name: str
    requests: int = 0
    replies: int = 0
    cost: float = 0.0
    tokens: int = 0


@dataclass
class ReportData:
    """财报数据快照。"""

    date_text: str
    total_requests: int = 0
    total_replies: int = 0
    total_cost: float = 0.0
    total_tokens: int = 0
    model_costs: list = field(default_factory=list)


def today_bucket(timestamps: Any, values: Any) -> float:
    """把 (时间标签序列, 数值序列) 中属于「今天」的数值求和。

    timestamps 形如 ["09-01 00:00", "09-01 01:00", ...] 或 ISO/epoch 字符串;
    无法解析时间戳的桶不计入(宁可少报不重复计入昨天)。
    """
    if not isinstance(values, (list, tuple)):
        return 0.0
    labels = timestamps if isinstance(timestamps, (list, tuple)) else []
    today = datetime.now().date()
    total = 0.0
    for i, value in enumerate(values):
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        label = str(labels[i]) if i < len(labels) else ""
        if _label_is_today(label, today):
            total += num
    return total


def _label_is_today(label: str, today) -> bool:
    text = (label or "").strip()
    if not text:
        return False
    # 纯 epoch 秒/毫秒
    if text.replace(".", "").isdigit():
        try:
            ts = float(text)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts).date() == today
        except (ValueError, OSError, OverflowError):
            return False
    # 常见格式逐一尝试; 只比较「日期部分是否为今天」
    date_part = text.split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"):
        try:
            parsed = datetime.strptime(date_part, fmt)
            if fmt in ("%m-%d", "%m/%d"):
                return (parsed.month, parsed.day) == (today.month, today.day)
            return parsed.date() == today
        except ValueError:
            continue
    # ISO 带时区
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        return parsed.date() == today
    except ValueError:
        return False


def series_total(raw: Any, today_only: bool = True) -> float:
    """从 statistics trend 返回结构里提取总和。

    兼容 {timestamps, values_by_key} / {timestamps, values} / {total} 三种形态。
    """
    if not isinstance(raw, dict):
        return 0.0
    timestamps = raw.get("timestamps") or raw.get("time_labels") or raw.get("labels")
    direct = raw.get("total")
    if isinstance(direct, (int, float)) and not today_only:
        return float(direct)
    total = 0.0
    by_key = raw.get("values_by_key") or raw.get("series") or raw.get("data_by_key")
    if isinstance(by_key, dict):
        for values in by_key.values():
            total += today_bucket(timestamps, values)
        return total
    values = raw.get("values") or raw.get("data")
    if values is not None:
        total += today_bucket(timestamps, values)
    return total


def series_by_model(raw: Any) -> dict:
    """从 model_trend 的 values_by_key 提取 {模型名: 今日总和}。"""
    if not isinstance(raw, dict):
        return {}
    timestamps = raw.get("timestamps") or raw.get("time_labels") or raw.get("labels")
    by_key = raw.get("values_by_key") or raw.get("series") or raw.get("data_by_key")
    if not isinstance(by_key, dict):
        return {}
    labels_by_key = raw.get("labels_by_key") or raw.get("label_by_key") or {}
    out: dict = {}
    for key, values in by_key.items():
        label = labels_by_key.get(key) if isinstance(labels_by_key, dict) and labels_by_key.get(key) else key
        out[str(label)] = today_bucket(timestamps, values)
    return out


def merge_model_stats(cost_by_model: dict, req_by_model: dict, tokens_by_model: dict | None = None) -> list:
    """合并成本、请求次数与 token 三张表为 ModelCost 列表, 按成本降序。"""
    merged: dict = {}
    for name, cost in cost_by_model.items():
        merged.setdefault(name, ModelCost(name=name)).cost = float(cost)
    for name, req in req_by_model.items():
        stat = merged.setdefault(name, ModelCost(name=name))
        if stat.requests <= 0:
            stat.requests = int(req)
    for name, tokens in (tokens_by_model or {}).items():
        stat = merged.setdefault(name, ModelCost(name=name))
        stat.tokens = int(tokens)
    return sorted(
        (s for s in merged.values() if s.cost > 0 or s.requests > 0 or s.tokens > 0),
        key=lambda s: s.cost,
        reverse=True,
    )


def fmt_int(value: float | int) -> str:
    """千分位整数格式化。"""
    return f"{int(round(value)):,}"


def seconds_until(time_text: str) -> float:
    now = datetime.now()
    try:
        hour, minute = (int(p) for p in time_text.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        hour, minute = 23, 30
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 1.0)


def build_report_html(data: ReportData, title: str) -> str:
    subtitle = "今日 0 点至当前的模型调用概览"
    rows = data.model_costs or [ModelCost(name="暂无模型记录")]
    max_cost = max([item.cost for item in rows] + [0.01])
    body_rows = "\n".join(_model_row_html(item, max_cost) for item in rows[:12])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; width: 900px; font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif;
       color: #202124; background: #f4f7f8; }}
.sheet {{ padding: 54px; background: linear-gradient(180deg, #ffffff 0%, #eef4f5 100%); }}
.head {{ border-left: 10px solid #0f766e; padding-left: 24px; margin-bottom: 34px; }}
.kicker {{ font-size: 28px; color: #52605f; margin-bottom: 8px; }}
h1 {{ margin: 0; font-size: 54px; line-height: 1.14; }}
.subtitle {{ margin-top: 12px; font-size: 26px; color: #56616a; }}
.metrics {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px; margin: 34px 0; }}
.metric {{ background: #ffffff; border: 1px solid #d9e3e4; border-radius: 8px; padding: 22px; }}
.label {{ font-size: 22px; color: #667478; }}
.value {{ margin-top: 10px; font-size: 38px; font-weight: 700; color: #0b3b3f; }}
.section-title {{ font-size: 30px; font-weight: 700; margin: 40px 0 18px; }}
.row {{ background: #ffffff; border: 1px solid #dce5e6; border-radius: 8px; padding: 18px 20px; margin-bottom: 12px; }}
.row-top {{ display: flex; align-items: baseline; justify-content: space-between; gap: 18px; }}
.model {{ min-width: 0; overflow-wrap: anywhere; font-size: 24px; font-weight: 700; }}
.cost {{ flex: 0 0 auto; font-size: 24px; color: #0f766e; font-weight: 700; }}
.bar {{ height: 12px; margin: 14px 0 8px; border-radius: 99px; background: #dde7e8; overflow: hidden; }}
.bar span {{ display: block; height: 100%; background: #0f766e; }}
.minor {{ font-size: 20px; color: #657276; }}
.footer {{ margin-top: 36px; padding-top: 20px; border-top: 1px solid #cad7d8; font-size: 22px; color: #52605f; }}
</style>
</head>
<body>
<main class="sheet">
  <section class="head">
    <div class="kicker">{html.escape(data.date_text)}</div>
    <h1>{html.escape(title)}</h1>
    <div class="subtitle">{html.escape(subtitle)}</div>
  </section>
  <section class="metrics">
    <div class="metric"><div class="label">累计请求</div><div class="value">{data.total_requests}</div></div>
    <div class="metric"><div class="label">回复消息</div><div class="value">{data.total_replies}</div></div>
    <div class="metric"><div class="label">回复成本</div><div class="value">{data.total_cost:.4f} 元</div></div>
    <div class="metric"><div class="label">消耗 Token</div><div class="value">{fmt_int(data.total_tokens)}</div></div>
  </section>
  <div class="section-title">各模型回复成本</div>
  {body_rows}
  <div class="footer">净收入：-{data.total_cost:.4f} 元。数据来自 MaiBot 本地统计接口。</div>
</main>
</body>
</html>"""


def _model_row_html(item: ModelCost, max_cost: float) -> str:
    width = max(4, min(100, int(item.cost / max_cost * 100)))
    detail = f"请求 {item.requests} 次"
    if item.replies > 0:
        detail += f" / 回复 {item.replies} 条"
    if item.tokens > 0:
        detail += f" / {fmt_int(item.tokens)} tokens"
    return f"""<div class="row">
  <div class="row-top">
    <div class="model">{html.escape(item.name)}</div>
    <div class="cost">{item.cost:.4f} 元</div>
  </div>
  <div class="bar"><span style="width:{width}%"></span></div>
  <div class="minor">{detail}</div>
</div>"""


def extract_image_base64(image: Any) -> str:
    """从 render.html2png 的返回值里提取 base64。"""
    if isinstance(image, bytes):
        return base64.b64encode(image).decode("utf-8")
    if isinstance(image, dict):
        for key in ("image_base64", "base64", "data", "content"):
            value = image.get(key)
            if isinstance(value, str) and value.strip():
                return _strip_data_url(value)
            if isinstance(value, bytes):
                return base64.b64encode(value).decode("utf-8")
        for key in ("path", "file_path", "filename"):
            value = image.get(key)
            if isinstance(value, str) and value.strip():
                try:
                    from pathlib import Path

                    path = Path(value)
                    if path.is_file():
                        return base64.b64encode(path.read_bytes()).decode("utf-8")
                except Exception:
                    return ""
    if isinstance(image, str):
        value = image.strip()
        if value and not value.startswith("<!doctype"):
            return _strip_data_url(value)
    return ""


def _strip_data_url(value: str) -> str:
    value = value.strip()
    if value.startswith("data:image/") and "," in value:
        return value.split(",", 1)[1].strip()
    return value


def make_forward_node(segment_type: str, content: str) -> dict:
    return {
        "user_id": "0",
        "nickname": "麦麦",
        "segments": [{"type": segment_type, "content": content}],
    }


# ---------- 配置模型 ----------


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件设置"

    config_version: str = Field(default="1", description="配置版本号（热更新迁移用，勿手动修改）")
    enabled: bool = Field(default=True, description="是否启用插件")


class ReportSection(PluginConfigBase):
    __ui_label__ = "财报"

    title: str = Field(default="今日模型调用财报", description="财报标题")
    use_forward_message: bool = Field(default=True, description="使用合并转发消息发送（关闭则逐条普通消息）")
    opening: str = Field(
        default="{date}模型调用财报已生成，以下是今日请求次数、回复量与模型成本汇总。",
        description="财报开头文本，可用 {date} 占位符",
    )


class PermissionSection(PluginConfigBase):
    __ui_label__ = "权限"

    query_admin_only: bool = Field(default=False, description="查询命令仅管理员可用")
    admins: list = Field(default_factory=list, description="管理员 QQ 号列表")


class SchedulerSection(PluginConfigBase):
    __ui_label__ = "定时发送"

    enabled: bool = Field(default=False, description="启用每日定时发送")
    time: str = Field(default="23:30", description="定时发送时间（HH:MM）")
    group_ids: list = Field(default_factory=list, description="定时推送的 QQ 群号列表")
    private_ids: list = Field(default_factory=list, description="定时推送的私聊 QQ 号列表")


class ExpensesSummaryConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    report: ReportSection = Field(default_factory=ReportSection)
    permission: PermissionSection = Field(default_factory=PermissionSection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)


# ---------- 插件主体 ----------


class ExpensesSummaryPlugin(MaiBotPlugin):
    """今日模型花费总结。"""

    config_model = ExpensesSummaryConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._scheduler_task: Optional[asyncio.Task] = None

    async def on_load(self) -> None:
        self._self_check()
        if self.config.scheduler.enabled:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            self.ctx.logger.info("定时财报已启用，每天 %s 发送", self.config.scheduler.time)

    async def on_unload(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
        self.ctx.logger.info("今日模型花费总结插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "self":
            self.ctx.logger.info("配置已更新: version=%s", version)
            # 定时器按新配置重建
            if self._scheduler_task:
                self._scheduler_task.cancel()
                try:
                    await self._scheduler_task
                except asyncio.CancelledError:
                    pass
                self._scheduler_task = None
            if self.config.scheduler.enabled:
                self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    # ---- 行为自检: 启动时用固定样例验证纯函数, 结果打进日志 ----
    def _self_check(self) -> None:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            ts = [f"{today} 0{i}:00" for i in range(3)] + ["2000-01-01 00:00"]
            vals = [1, 2, 3, 100]
            assert today_bucket(ts, vals) == 6.0, "today_bucket 过滤错误"
            assert series_total({"timestamps": ts, "values_by_key": {"m": vals}}) == 6.0, "series_total 错误"
            merged = merge_model_stats({"gpt": 0.5}, {"gpt": 3, "glm": 1})
            assert merged[0].name == "gpt" and merged[0].cost == 0.5, "merge_model_stats 错误"
            assert seconds_until("00:01") > 0, "seconds_until 错误"
            self.ctx.logger.info("[自检] 统计口径函数 OK")
        except AssertionError as exc:
            self.ctx.logger.error("[自检] 行为异常: %s", exc)

    # ---- 命令 ----
    @Command("expenses", description="生成今日模型调用财报", pattern=r"^\s*[/／]\s*(?:expenses|今日财报)\s*$")
    async def cmd_expenses(self, **kwargs) -> tuple:
        stream_id = self._stream_id(kwargs)
        if self.config.permission.query_admin_only and not self._is_admin(kwargs):
            text = "你没有权限使用财报查询命令"
            await self._send_text(text, stream_id)
            return True, text, 2 if stream_id else 0
        sent = await self.send_report(stream_id)
        text = "已发送今日模型调用财报" if sent else "财报生成或发送失败，请查看日志"
        return True, text, 2 if (sent and stream_id) else 0

    # ---- Tool: 让 LLM 主动调用 ----
    @Tool(
        "expenses_summary",
        brief_description="生成并发送今日模型调用次数、token 消耗与成本财报",
        detailed_description="生成今日模型调用/回复/成本/token 消耗统计的财报图片并发送到当前聊天，"
        "适用于「总结今日花费」「公开模型成本」等场景。无需参数。",
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=True,
            ),
        ],
    )
    async def tool_expenses_summary(self, stream_id: str = "", **kwargs):
        target = stream_id or self._stream_id(kwargs)
        sent = await self.send_report(target)
        return {
            "success": sent,
            "message": "今日模型调用财报已生成并发送，不要额外复述财报内容。" if sent else "财报生成或发送失败。",
        }

    # ---- 核心: 采集 + 渲染 + 发送 ----
    async def collect_report_data(self) -> ReportData:
        local = self.ctx.statistics.local
        costs_raw = await local.model_trend(days=1, bucket="hour", top_models=50, metric="cost")
        requests_raw = await local.model_trend(days=1, bucket="hour", top_models=50, metric="request")
        messages_raw = await local.message_trend(days=1, bucket="hour", top_chats=50)
        tokens_raw = await self._fetch_token_trend(local)

        cost_by = series_by_model(costs_raw)
        req_by = series_by_model(requests_raw)
        tokens_by = series_by_model(tokens_raw)
        model_costs = merge_model_stats(cost_by, req_by, tokens_by)

        total_requests = int(sum(s.requests for s in model_costs))
        total_cost = sum(s.cost for s in model_costs)
        if total_requests <= 0:
            total_requests = int(series_total(requests_raw))
        if total_cost <= 0:
            total_cost = series_total(costs_raw)
        total_replies = int(series_total(messages_raw))
        total_tokens = int(round(sum(s.tokens for s in model_costs)))
        if total_tokens <= 0:
            total_tokens = int(round(series_total(tokens_raw)))

        return ReportData(
            date_text=datetime.now().strftime("%Y年%m月%d日"),
            total_requests=total_requests,
            total_replies=total_replies,
            total_cost=total_cost,
            total_tokens=total_tokens,
            model_costs=model_costs,
        )

    async def _fetch_token_trend(self, local) -> dict:
        """拉取今日 token 趋势; 优先按模型分组, 失败时退回整体趋势。"""
        try:
            return await local.token_trend(days=1, bucket="hour", group_by="model", top_items=50)
        except Exception as exc:
            self.ctx.logger.warning("token_trend(group_by=model) 失败, 退回整体趋势: %s", exc)
        try:
            return await local.token_trend(days=1, bucket="hour", top_items=50)
        except Exception as exc:
            self.ctx.logger.warning("token_trend 拉取失败, 本次财报不含 token: %s", exc)
        return {}

    async def send_report(self, stream_id: str = "") -> bool:
        if not self.config.plugin.enabled:
            return False
        if not stream_id:
            self.ctx.logger.error("发送财报失败: 缺少 stream_id")
            return False
        config = self.config
        try:
            data = await self.collect_report_data()
            image = await self._render_image(data)
            nodes = self._build_nodes(data, image)
        except Exception as exc:
            self.ctx.logger.error("财报生成失败: %s", exc, exc_info=True)
            return False

        if config.report.use_forward_message:
            return await self._send_forward(nodes, stream_id)
        return await self._send_plain(nodes, stream_id)

    async def _render_image(self, data: ReportData):
        html_doc = build_report_html(data, self.config.report.title)
        try:
            result = await self.ctx.render.html2png(
                html_doc,
                selector=".sheet",
                viewport={"width": 900, "height": 1200},
                full_page=True,
            )
        except Exception as exc:
            self.ctx.logger.warning("html2png 渲染失败，退回文本财报: %s", exc)
            return None
        return result

    def _build_nodes(self, data: ReportData, image: Any) -> list:
        nodes = [make_forward_node("text", self._opening_text(data))]
        encoded = extract_image_base64(image) if image is not None else ""
        if encoded:
            nodes.append(make_forward_node("image", encoded))
        else:
            nodes.append(make_forward_node("text", self._plain_table(data)))
        return nodes

    def _opening_text(self, data: ReportData) -> str:
        template = self.config.report.opening
        return template.replace("{date}", data.date_text)

    def _plain_table(self, data: ReportData) -> str:
        lines = [
            f"累计请求 {data.total_requests} 次 / 回复 {data.total_replies} 条 / "
            f"成本 {data.total_cost:.4f} 元 / Token {fmt_int(data.total_tokens)}"
        ]
        for item in data.model_costs[:12]:
            token_text = f" / {fmt_int(item.tokens)} tokens" if item.tokens > 0 else ""
            lines.append(f"· {item.name}: {item.cost:.4f} 元（请求 {item.requests} 次{token_text}）")
        return "\n".join(lines) if len(lines) > 1 else "今日暂无模型调用记录。"

    async def _send_forward(self, nodes: list, stream_id: str) -> bool:
        try:
            sent = await self.ctx.send.forward(nodes, stream_id)
            return bool(sent)
        except Exception as exc:
            self.ctx.logger.warning("合并转发失败，退回逐条发送: %s", exc)
            return await self._send_plain(nodes, stream_id)

    async def _send_plain(self, nodes: list, stream_id: str) -> bool:
        sent_any = False
        for node in nodes:
            for segment in node["segments"]:
                try:
                    if segment["type"] == "text":
                        sent_any = bool(await self.ctx.send.text(segment["content"], stream_id)) or sent_any
                    elif segment["type"] == "image":
                        sent_any = bool(await self.ctx.send.image(segment["content"], stream_id)) or sent_any
                except Exception as exc:
                    self.ctx.logger.warning("财报消息发送失败: %s", exc)
        return sent_any

    async def _send_text(self, text: str, stream_id: str) -> bool:
        if not stream_id:
            self.ctx.logger.error("发送文本失败: 缺少 stream_id")
            return False
        try:
            return bool(await self.ctx.send.text(text, stream_id))
        except Exception as exc:
            self.ctx.logger.error("发送文本失败: %s", exc)
            return False

    # ---- 定时发送 ----
    async def _scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(seconds_until(self.config.scheduler.time))
            try:
                await self._send_scheduled()
            except Exception as exc:
                self.ctx.logger.error("定时发送财报失败: %s", exc, exc_info=True)

    async def _send_scheduled(self) -> None:
        group_ids = [str(g).strip() for g in (self.config.scheduler.group_ids or []) if str(g).strip()]
        private_ids = [str(u).strip() for u in (self.config.scheduler.private_ids or []) if str(u).strip()]
        if not group_ids and not private_ids:
            self.ctx.logger.warning("定时财报已启用，但未配置目标群号/私聊号")
            return
        for group_id in group_ids:
            stream_id = await self._resolve_stream("group", group_id)
            if stream_id:
                ok = await self.send_report(stream_id)
                self.ctx.logger.info("定时财报 -> 群 %s: %s", group_id, "成功" if ok else "失败")
            else:
                self.ctx.logger.error("定时财报失败: 无法解析群 %s 的会话", group_id)
        for user_id in private_ids:
            stream_id = await self._resolve_stream("private", user_id)
            if stream_id:
                ok = await self.send_report(stream_id)
                self.ctx.logger.info("定时财报 -> 私聊 %s: %s", user_id, "成功" if ok else "失败")
            else:
                self.ctx.logger.error("定时财报失败: 无法解析私聊 %s 的会话", user_id)

    async def _resolve_stream(self, chat_type: str, target_id: str) -> str:
        chat = self.ctx.chat
        try:
            if chat_type == "group":
                stream = await chat.get_stream_by_group_id(target_id)
            else:
                stream = await chat.get_stream_by_user_id(target_id)
            sid = self._stream_id(stream if isinstance(stream, dict) else {"stream_id": stream})
            if sid:
                return sid
        except Exception as exc:
            self.ctx.logger.warning("解析会话失败(%s %s): %s", chat_type, target_id, exc)
        try:
            kwargs = {"chat_type": chat_type}
            if chat_type == "group":
                kwargs["group_id"] = target_id
            else:
                kwargs["user_id"] = target_id
            stream = await chat.open_session(**kwargs)
            return self._stream_id(stream if isinstance(stream, dict) else {"stream_id": stream}) or ""
        except Exception as exc:
            self.ctx.logger.warning("open_session 失败(%s %s): %s", chat_type, target_id, exc)
        return ""

    # ---- 工具方法 ----
    @staticmethod
    def _stream_id(obj: Any) -> str:
        """从命令 kwargs / 会话对象里尽力提取 stream_id（字段名随版本有差异）。"""
        candidates: list = []
        if isinstance(obj, dict):
            candidates.append(obj)
            for key in ("message", "chat_stream", "event", "context"):
                if isinstance(obj.get(key), dict):
                    candidates.append(obj[key])
        for cand in candidates:
            for key in ("stream_id", "chat_id", "session_id", "stream"):
                value = cand.get(key)
                if isinstance(value, dict):
                    value = value.get("stream_id")
                if value:
                    return str(value)
        return ""

    def _is_admin(self, kwargs: dict) -> bool:
        user_id = ""
        for source in (kwargs, kwargs.get("message") if isinstance(kwargs.get("message"), dict) else {}):
            for key in ("user_id", "sender_id", "from_user_id", "qq"):
                if source.get(key):
                    user_id = str(source[key])
                    break
            if user_id:
                break
        admins = {str(a).strip() for a in (self.config.permission.admins or []) if str(a).strip()}
        return bool(user_id and user_id in admins)


def create_plugin():
    return ExpensesSummaryPlugin()
