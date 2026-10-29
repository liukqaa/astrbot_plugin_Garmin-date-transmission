"""佳明训练与身体状况数据插件（Garmin Connect 官方 Personal API）。

只读接入，支持中国大陆站（personal.connectapi.garmin.cn）与国际站
（personal.connectapi.garmin.com）。全部接口与字段结构均于 2026-10-29 用真实
账号实测确认。

实测要点（很重要，网上文档大多写错）：
- token 端点返回 **text/plain 的 JWT 字符串**，不是 JSON；
- 若带 `Accept: application/json`，token 端点返回 HTTP 406 NotAcceptableException；
- Personal API **没有** `/activity-service/activity/{id}/splits`（404），
  分段信息在活动详情的 `splitSummaries` 字段里；
- Personal API **不提供**训练状态(Training Status)、训练准备度、VO2max 区间、
  比赛预测接口（均 404），这些只能走网页内部接口；
- 血氧字段是 `averageSpO2`/`lowestSpO2`；压力在 `values.overallStressLevel`；
  每日汇总在 `data[0].movement.steps.value` 等多层嵌套里。

其他设计：
- token 内存缓存，过期自动重换，401 时自动重试一次；
- 全部请求使用 httpx 异步客户端，符合 AstrBot 插件规范；
- 工具失败返回可读错误字符串，不抛异常中断对话；
- 输出按字符数截断，避免撑爆上下文。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

# 区域 -> Personal API 基地址
REGION_BASE_URL = {
    "cn": "https://personal.connectapi.garmin.cn",
    "global": "https://personal.connectapi.garmin.com",
}

TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

# token 缓存提前量：提前 30 分钟视为过期，避免临界失效
TOKEN_SAFETY_MARGIN = 30 * 60
# 拿不到明确有效期时的默认 TTL
DEFAULT_TOKEN_TTL = 24 * 3600

# 单次工具返回的最大字符数
MAX_OUTPUT_CHARS = 6000

# HRV 状态中文映射
HRV_STATUS_CN = {
    "BALANCED": "平衡",
    "UNBALANCED": "不平衡",
    "LOW": "偏低",
    "HIGH": "偏高",
    "POOR": "较差",
}

# 心率区间名称（Garmin 的 5 区模型）
HR_ZONE_NAMES = {
    1: "轻松/恢复",
    2: "有氧耐力",
    3: "有氧动力",
    4: "阈值",
    5: "无氧",
}


def _today() -> date:
    return datetime.now().astimezone().date()


def _parse_date(value: str | None, default: date | None = None) -> date:
    """解析 YYYY-MM-DD；为空时返回 default（通常是今天）。"""
    if not value:
        return default or _today()
    value = str(value).strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD，收到：{value}") from exc


def _daterange(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出过长已截断，共 {len(text)} 字符）"


def _fmt_duration(seconds: float | int | None) -> str:
    if not seconds:
        return "-"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m:02d}分"
    if m:
        return f"{m}分{s:02d}秒" if s else f"{m}分"
    return f"{s}秒"


def _fmt_pace(speed_mps: float | None) -> str:
    """速度(m/s) -> 配速 分'秒"/km。"""
    if not speed_mps or speed_mps <= 0:
        return "-"
    sec_per_km = 1000.0 / speed_mps
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}'{s:02d}\"/km"


def _fmt_km(meters: float | None) -> str:
    if meters is None:
        return "-"
    return f"{meters / 1000.0:.2f} km"


def _kv(pairs: dict[str, Any]) -> str:
    """拼成 '键值，键值'，自动跳过 None。"""
    return "，".join(f"{k}{v}" for k, v in pairs.items() if v is not None)


def _summarize_activity(act: dict[str, Any], detail: bool = False) -> str:
    start = str(act.get("startTimeLocal") or "").replace("T", " ")[:16]
    name = act.get("activityName") or "未命名活动"
    atype = (act.get("activityType") or {}).get("typeKey") or ""
    lines = [f"- {start} | {atype} | {name}"]
    lines.append(
        "  "
        + _kv(
            {
                "时长 ": _fmt_duration(act.get("duration")),
                "距离 ": _fmt_km(act.get("distance")),
                "均配速 ": _fmt_pace(act.get("averageSpeed")),
            }
        )
    )
    metric = _kv(
        {
            "平均心率 ": (
                f"{round(act['averageHR'])} bpm" if act.get("averageHR") else None
            ),
            "最大心率 ": (f"{round(act['maxHR'])} bpm" if act.get("maxHR") else None),
            "爬升 ": (
                f"{round(act['elevationGain'])} m" if act.get("elevationGain") else None
            ),
            "消耗 ": f"{round(act['calories'])} kcal" if act.get("calories") else None,
            "步频 ": (
                f"{round(act['averageRunningCadenceInStepsPerMinute'])} spm"
                if act.get("averageRunningCadenceInStepsPerMinute")
                else None
            ),
        }
    )
    if metric:
        lines.append("  " + metric)
    if detail:
        extra = _kv(
            {
                "训练效果 ": (
                    round(act["aerobicTrainingEffect"], 1)
                    if act.get("aerobicTrainingEffect")
                    else None
                ),
                "训练负荷 ": (
                    round(act["activityTrainingLoad"])
                    if act.get("activityTrainingLoad")
                    else None
                ),
                "步数 ": act.get("steps") or None,
            }
        )
        if extra:
            lines.append("  " + extra)
    lines.append(f"  activityId={act.get('activityId')}")
    return "\n".join(lines)


def _summarize_splits(splits: list[dict[str, Any]]) -> list[str]:
    """detail.splitSummaries —— 按 splitType 汇总的分段信息。

    Garmin 会同时返回 INTERVAL_ACTIVE 与 RWD_RUN 等不同分型，数值常常完全一致，
    这里按 (距离, 时长) 去重，避免同一段重复展示。
    """
    lines: list[str] = []
    seen: set[tuple[Any, Any]] = set()
    for sp in splits[:10]:
        key = (sp.get("distance"), sp.get("duration"))
        if key in seen:
            continue
        seen.add(key)
        split_type = sp.get("splitType") or "分段"
        lines.append(
            "    "
            + str(split_type)
            + "："
            + _kv(
                {
                    "距离 ": _fmt_km(sp.get("distance")),
                    "时长 ": _fmt_duration(sp.get("duration")),
                    "均配速 ": _fmt_pace(sp.get("averageSpeed")),
                    "平均心率 ": (
                        f"{round(sp['averageHR'])} bpm" if sp.get("averageHR") else None
                    ),
                    "爬升 ": (
                        f"{round(sp['elevationGain'])} m"
                        if sp.get("elevationGain")
                        else None
                    ),
                }
            )
        )
    return lines


def _pick_sleep_summary(sleep: dict[str, Any]) -> dict[str, Any]:
    """睡眠：Personal API 返回扁平结构（不是 dailySleepDTO 嵌套）。"""
    dto = sleep.get("dailySleepDTO") or sleep
    if not dto:
        return {}
    scores = dto.get("sleepScores")
    score = None
    if isinstance(scores, dict):
        overall = scores.get("overall")
        if isinstance(overall, dict):
            score = overall.get("value")
        elif isinstance(overall, (int, float)):
            score = overall
    out = {
        "睡眠评分 ": score,
        "总睡眠 ": (
            _fmt_duration(dto.get("sleepTimeSeconds"))
            if dto.get("sleepTimeSeconds")
            else None
        ),
        "深睡 ": (
            _fmt_duration(dto.get("deepSleepSeconds"))
            if dto.get("deepSleepSeconds")
            else None
        ),
        "浅睡 ": (
            _fmt_duration(dto.get("lightSleepSeconds"))
            if dto.get("lightSleepSeconds")
            else None
        ),
        "REM ": (
            _fmt_duration(dto.get("remSleepSeconds"))
            if dto.get("remSleepSeconds")
            else None
        ),
        "清醒 ": (
            _fmt_duration(dto.get("awakeSleepSeconds"))
            if dto.get("awakeSleepSeconds")
            else None
        ),
        "睡眠心率 ": (
            f"{round(dto['avgHeartRate'])} bpm" if dto.get("avgHeartRate") else None
        ),
        "睡眠压力 ": dto.get("avgSleepStress"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _pick_hr_summary(hr: dict[str, Any]) -> dict[str, Any]:
    out = {
        "静息心率 ": hr.get("restingHeartRate"),
        "最高心率 ": hr.get("maxHeartRate"),
        "最低心率 ": hr.get("minHeartRate"),
        "7日均静息 ": hr.get("lastSevenDaysAvgRestingHeartRate"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _pick_hrv_summary(hrv: dict[str, Any]) -> dict[str, Any]:
    info = hrv.get("hrvSummary") or {}
    if not info:
        return {}
    status = info.get("status")
    baseline = info.get("baseline") or {}
    out = {
        "夜间HRV ": info.get("lastNightAvg"),
        "周均HRV ": info.get("weeklyAvg"),
        "基线 ": (
            f"{baseline.get('lowUpper')}-{baseline.get('balancedUpper')}"
            if baseline.get("lowUpper") is not None
            and baseline.get("balancedUpper") is not None
            else None
        ),
        "状态 ": f"{status}（{HRV_STATUS_CN.get(status, '')}）" if status else None,
    }
    return {k: v for k, v in out.items() if v is not None}


def _pick_stress_summary(stress: Any) -> dict[str, Any]:
    """压力：返回 [{"calendarDate":..., "values":{...}}]。"""
    arr = stress if isinstance(stress, list) else [stress]
    rows = [x for x in (arr or []) if isinstance(x, dict)]
    if not rows:
        return {}
    values = rows[-1].get("values") or {}
    out = {
        "平均压力 ": values.get("overallStressLevel"),
        "高压时长 ": (
            _fmt_duration(values.get("highStressDuration"))
            if values.get("highStressDuration")
            else None
        ),
        "静息时长 ": (
            _fmt_duration(values.get("restStressDuration"))
            if values.get("restStressDuration")
            else None
        ),
    }
    return {k: v for k, v in out.items() if v is not None}


def _pick_body_battery(report: Any) -> dict[str, Any]:
    arr = report if isinstance(report, list) else [report]
    rows = [x for x in (arr or []) if isinstance(x, dict)]
    if not rows:
        return {}
    row = rows[-1]
    values = row.get("bodyBatteryValuesArray") or []
    nums = [
        v[1]
        for v in values
        if isinstance(v, (list, tuple)) and len(v) > 1 and isinstance(v[1], int)
    ]
    out = {
        "充满 ": row.get("charged"),
        "消耗 ": row.get("drained"),
        "最高 ": max(nums) if nums else None,
        "最低 ": min(nums) if nums else None,
        "最新 ": nums[-1] if nums else None,
    }
    return {k: v for k, v in out.items() if v is not None}


def _pick_spo2(spo2: dict[str, Any]) -> dict[str, Any]:
    """血氧：真实字段是 averageSpO2 / lowestSpO2 / lastSevenDaysAvgSpO2。"""
    out = {
        "平均血氧 ": (
            f"{spo2['averageSpO2']}%" if spo2.get("averageSpO2") else None
        ),
        "最低血氧 ": f"{spo2['lowestSpO2']}%" if spo2.get("lowestSpO2") else None,
        "7日均血氧 ": (
            f"{spo2['lastSevenDaysAvgSpO2']}%"
            if spo2.get("lastSevenDaysAvgSpO2")
            else None
        ),
    }
    return {k: v for k, v in out.items() if v is not None}


def _pick_daily(daily: Any) -> dict[str, Any]:
    """v2/dailysummaries 返回 {"data":[...]}，字段多层嵌套。"""
    rows: list[dict[str, Any]] = []
    if isinstance(daily, dict):
        data = daily.get("data")
        if isinstance(data, list):
            rows = [x for x in data if isinstance(x, dict)]
        else:
            rows = [daily]
    elif isinstance(daily, list):
        rows = [x for x in daily if isinstance(x, dict)]
    if not rows:
        return {}
    row = rows[0]
    movement = row.get("movement") or {}
    calories = row.get("calories") or {}
    heart = row.get("heartRate") or {}
    intensity = row.get("intensityMinutes") or {}
    steps = movement.get("steps") or {}
    out = {
        "步数 ": steps.get("value"),
        "步数目标 ": steps.get("goal"),
        "活动热量 ": calories.get("burnedActive"),
        "静息心率 ": heart.get("restingValue"),
        "中强度分钟 ": intensity.get("moderate"),
        "高强度分钟 ": intensity.get("vigorous"),
    }
    return {k: v for k, v in out.items() if v is not None}


@register(
    "astrbot_plugin_garmin",
    "astrbot",
    "佳明训练与身体状况数据查询（Garmin Connect 官方 Personal API，只读）",
    "1.1.0",
)
class GarminPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        self._push_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ 基础
    async def initialize(self):
        """插件加载时调用：初始化 HTTP 客户端，并按配置启动定时推送。"""
        self._ensure_client()
        if self._push_enabled():
            self._push_task = asyncio.create_task(self._push_loop())
            logger.info(
                "[garmin] 定时推送已启动：每日 %s，每周 %s %s（会话 %s）",
                self.config.get("push_time", "08:00"),
                self.config.get("weekly_weekday", 1),
                self.config.get("weekly_time", "08:30"),
                self.config.get("push_umo", ""),
            )
        logger.info("[garmin] 插件已加载")

    async def terminate(self):
        if self._push_task is not None:
            self._push_task.cancel()
            try:
                await self._push_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._push_task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=15.0),
                follow_redirects=True,
                headers={"User-Agent": "AstrBot-Garmin-Plugin/1.1"},
            )
        return self._http

    def _region(self) -> str:
        region = str(self.config.get("region", "cn") or "cn").strip().lower()
        return region if region in REGION_BASE_URL else "cn"

    def _base_url(self) -> str:
        return REGION_BASE_URL[self._region()]

    def _api_key(self) -> str | None:
        key = self.config.get("api_key")
        if not key:
            return None
        key = str(key).strip()
        return key or None

    async def _get_token(self) -> str:
        """用 Personal API Key 换取 token，带缓存与并发保护。

        实测：token 端点返回 text/plain 的 JWT 字符串；
        带 `Accept: application/json` 会得到 HTTP 406 NotAcceptableException。
        """
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError(
                "未配置佳明 Personal API Key，请在插件配置页填写 api_key。"
            )
        now = time.time()
        if self._token and now < self._token_exp - TOKEN_SAFETY_MARGIN:
            return self._token

        async with self._token_lock:
            now = time.time()
            if self._token and now < self._token_exp - TOKEN_SAFETY_MARGIN:
                return self._token

            resp = await self._ensure_client().post(
                self._base_url() + TOKEN_PATH,
                headers={"Authorization": f"ApiKey {api_key}"},
            )
            if resp.status_code != 200:
                body = (resp.text or "")[:300]
                hint = ""
                if resp.status_code == 401:
                    hint = (
                        "（Key 无效或已过期，请到 connect.garmin.cn 的"
                        " 设置 → Personal API Key 重新生成）"
                    )
                elif resp.status_code == 526:
                    hint = "（大陆 Personal API 端点暂不可用，请稍后再试）"
                elif resp.status_code == 406:
                    hint = "（令牌端点不接受 Accept: application/json）"
                raise RuntimeError(
                    f"换取 token 失败：HTTP {resp.status_code}{hint} {body}"
                )

            text = (resp.text or "").strip()
            token: str | None = None
            content_type = (resp.headers.get("content-type") or "").lower()
            if "json" in content_type or text.startswith("{"):
                try:
                    data = resp.json()
                    token = (
                        data.get("access_token")
                        or data.get("accessToken")
                        or (data.get("data") or {}).get("access_token")
                        or data.get("token")
                    )
                    expires_in = data.get("expires_in") or data.get("expiresIn")
                    if isinstance(expires_in, (int, float)) and expires_in > 0:
                        self._token_exp = now + float(expires_in)
                except Exception:  # noqa: BLE001 - 退回纯文本解析
                    token = None
            if not token:
                # text/plain 的裸 JWT
                token = text if text.count(".") == 2 else None
            if not token:
                raise RuntimeError(f"token 响应无法解析：{text[:200]}")

            self._token = token
            if self._token_exp <= now:
                self._token_exp = now + DEFAULT_TOKEN_TTL
            logger.info(
                f"[garmin] 已获取 token（{self._region()}），"
                f"有效期约 {int((self._token_exp - now) / 60)} 分钟"
            )
            return token

    async def _get_json(self, path: str) -> Any:
        """携带 bearer token 的 GET 请求，token 失效(401)时自动重换一次。"""
        token = await self._get_token()
        client = self._ensure_client()
        resp = await client.get(
            self._base_url() + path,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            self._token = None
            self._token_exp = 0.0
            token = await self._get_token()
            resp = await client.get(
                self._base_url() + path,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"请求 {path} 失败：HTTP {resp.status_code} {(resp.text or '')[:200]}"
            )
        if not resp.content:
            return None
        return resp.json()

    @staticmethod
    def _err(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPError):
            return f"佳明接口网络错误：{exc.__class__.__name__}: {exc}"
        return f"查询失败：{exc}"

    # -------------------------------------------------------------- LLM 工具
    @filter.llm_tool(name="garmin_activities")
    async def garmin_activities(
        self,
        event: AstrMessageEvent,
        start_date: str = "",
        end_date: str = "",
        limit: int = 10,
    ):
        '''查询佳明账号在指定日期范围内的训练活动（跑步、骑行、游泳等）摘要列表。

        Args:
            start_date(string): 开始日期，格式 YYYY-MM-DD；留空表示从 end_date 往前 30 天
            end_date(string): 结束日期，格式 YYYY-MM-DD；留空表示今天
            limit(number): 最多返回多少条活动，默认 10，最大 50
        '''
        try:
            end = _parse_date(end_date)
            start = (
                _parse_date(start_date, default=end - timedelta(days=30))
                if start_date
                else end - timedelta(days=30)
            )
            n = max(1, min(int(limit or 10), 50))
            path = (
                "/activitylist-service/activities/search/activities"
                f"?start=0&limit={n}"
                f"&startDate={start.isoformat()}&endDate={end.isoformat()}"
            )
            data = await self._get_json(path)
            acts = data if isinstance(data, list) else (data or {}).get("activityList")
            if not acts:
                return f"{start} 至 {end} 没有查到训练活动记录。"
            header = f"{start} 至 {end} 共 {len(acts)} 条活动："
            body = "\n".join(_summarize_activity(a, detail=True) for a in acts)
            return _clip(header + "\n" + body)
        except Exception as exc:  # noqa: BLE001 - 工具需优雅降级
            return self._err(exc)

    @filter.llm_tool(name="garmin_activity_detail")
    async def garmin_activity_detail(self, event: AstrMessageEvent, activity_id: str):
        '''查询某一次训练活动的详细信息，包括概要指标与分段摘要。

        Args:
            activity_id(string): 活动 ID，可从 garmin_activities 的结果中获取
        '''
        try:
            try:
                aid = int(str(activity_id).strip())
            except ValueError as exc:
                raise ValueError(f"activity_id 必须是数字，收到：{activity_id}") from exc
            detail = await self._get_json(f"/activity-service/activity/{aid}")
            if not detail:
                return f"未找到活动 {aid}。"
            summary = detail.get("summaryDTO") or {}
            merged: dict[str, Any] = {
                "activityId": detail.get("activityId") or aid,
                "activityName": detail.get("activityName"),
                "startTimeLocal": summary.get("startTimeLocal"),
                "activityType": detail.get("activityTypeDTO") or {},
            }
            merged.update(summary)
            lines = [_summarize_activity(merged, detail=True)]
            splits = detail.get("splitSummaries") or []
            if splits:
                lines.append(f"  分段摘要（{len(splits)} 组）：")
                lines.extend(_summarize_splits(splits))
            return _clip("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    @filter.llm_tool(name="garmin_body_composition")
    async def garmin_body_composition(
        self,
        event: AstrMessageEvent,
        start_date: str = "",
        end_date: str = "",
    ):
        '''查询身体成分记录：体重、BMI、体脂率、肌肉量、骨量、体水分等（来自佳明体重秤或手动录入）。

        Args:
            start_date(string): 开始日期，格式 YYYY-MM-DD；留空表示从 end_date 往前 30 天
            end_date(string): 结束日期，格式 YYYY-MM-DD；留空表示今天
        '''
        try:
            end = _parse_date(end_date)
            start = (
                _parse_date(start_date, default=end - timedelta(days=30))
                if start_date
                else end - timedelta(days=30)
            )
            data = await self._get_json(
                f"/weight-service/weight/range/{start.isoformat()}/{end.isoformat()}"
                "?includeAll=true"
            )
            records: list[dict[str, Any]] = []
            if isinstance(data, dict):
                for daily in data.get("dailyWeightSummaries") or []:
                    if not isinstance(daily, dict):
                        continue
                    values = daily.get("weightValues")
                    if isinstance(values, list):
                        records.extend(v for v in values if isinstance(v, dict))
                    elif daily.get("weight") is not None:
                        records.append(daily)
                if not records:
                    for key in ("dateWeightList", "allWeightMetrics"):
                        items = data.get(key)
                        if isinstance(items, list) and items:
                            records.extend(i for i in items if isinstance(i, dict))
                            break
            elif isinstance(data, list):
                records = [x for x in data if isinstance(x, dict)]

            if not records:
                return (
                    f"{start} 至 {end} 没有查到身体成分记录。"
                    "（需连接佳明 Index 体重秤，或在 Garmin Connect App 手动录入体重）"
                )

            def _ts(rec: dict[str, Any]) -> str:
                raw = (
                    rec.get("date")
                    or rec.get("timestampGMT")
                    or rec.get("summaryDate")
                )
                return str(raw)[:10] if raw else "-"

            records.sort(key=_ts, reverse=True)
            lines = [f"{start} 至 {end} 身体成分记录 {len(records)} 条（最新在前）："]
            for rec in records[:30]:
                weight_g = rec.get("weight")
                items = {
                    "体重 ": (
                        f"{weight_g / 1000:.2f} kg"
                        if isinstance(weight_g, (int, float))
                        else None
                    ),
                    "BMI ": (
                        f"{rec['bmi']:.1f}"
                        if isinstance(rec.get("bmi"), (int, float))
                        else None
                    ),
                    "体脂 ": (
                        f"{rec['bodyFat']:.1f}%"
                        if isinstance(rec.get("bodyFat"), (int, float))
                        else None
                    ),
                    "体水分 ": (
                        f"{rec['bodyWater']:.1f}%"
                        if isinstance(rec.get("bodyWater"), (int, float))
                        else None
                    ),
                    "肌肉量 ": (
                        f"{rec['muscleMass'] / 1000:.2f} kg"
                        if isinstance(rec.get("muscleMass"), (int, float))
                        else None
                    ),
                    "骨量 ": (
                        f"{rec['boneMass'] / 1000:.2f} kg"
                        if isinstance(rec.get("boneMass"), (int, float))
                        else None
                    ),
                    "内脏脂肪 ": rec.get("visceralFat"),
                    "代谢年龄 ": rec.get("metabolicAge"),
                }
                lines.append(f"  {_ts(rec)}  " + _kv(items))
            return _clip("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    @filter.llm_tool(name="garmin_health")
    async def garmin_health(
        self,
        event: AstrMessageEvent,
        start_date: str = "",
        end_date: str = "",
        metrics: str = "sleep,hr,hrv",
    ):
        '''查询身体状况数据：睡眠、心率、HRV、压力、Body Battery、血氧、每日汇总。

        Args:
            start_date(string): 开始日期，格式 YYYY-MM-DD；留空表示结束日期当天
            end_date(string): 结束日期，格式 YYYY-MM-DD；留空表示今天；最大跨度 14 天
            metrics(string): 逗号分隔，可选 sleep,hr,hrv,stress,bodybattery,spo2,daily，默认 sleep,hr,hrv
        '''
        try:
            end = _parse_date(end_date)
            start = _parse_date(start_date, default=end) if start_date else end
            days = _daterange(start, end)
            if len(days) > 14:
                days = days[-14:]
                start = days[0]
            wanted = {
                m.strip().lower() for m in (metrics or "").split(",") if m.strip()
            } or {"sleep", "hr", "hrv"}

            lines: list[str] = [f"{start} 至 {end} 身体状况："]
            errors: list[str] = []

            for d in days:
                iso = d.isoformat()
                block: list[str] = [f"[{iso}]"]

                if "sleep" in wanted:
                    try:
                        info = _pick_sleep_summary(
                            await self._get_json(
                                f"/sleep-service/sleep/dailySleep?date={iso}"
                            )
                            or {}
                        )
                        if info:
                            block.append("  睡眠：" + _kv(info))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"sleep {iso}: {exc}")

                if "hr" in wanted:
                    try:
                        info = _pick_hr_summary(
                            await self._get_json(
                                "/wellness-service/wellness/dailyHeartRate"
                                f"?date={iso}"
                            )
                            or {}
                        )
                        if info:
                            block.append("  心率：" + _kv(info))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"hr {iso}: {exc}")

                if "hrv" in wanted:
                    try:
                        info = _pick_hrv_summary(
                            await self._get_json(f"/hrv-service/hrv/{iso}") or {}
                        )
                        if info:
                            block.append("  HRV：" + _kv(info))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"hrv {iso}: {exc}")

                if "stress" in wanted:
                    try:
                        info = _pick_stress_summary(
                            await self._get_json(
                                f"/usersummary-service/stats/stress/daily/{iso}/{iso}"
                            )
                        )
                        if info:
                            block.append("  压力：" + _kv(info))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"stress {iso}: {exc}")

                if "bodybattery" in wanted:
                    try:
                        info = _pick_body_battery(
                            await self._get_json(
                                "/wellness-service/wellness/bodyBattery/reports/daily"
                                f"?startDate={iso}&endDate={iso}"
                            )
                        )
                        if info:
                            block.append("  BodyBattery：" + _kv(info))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"bodybattery {iso}: {exc}")

                if "spo2" in wanted:
                    try:
                        info = _pick_spo2(
                            await self._get_json(
                                "/wellness-service/wellness/daily/spo2acclimation/"
                                f"{iso}"
                            )
                            or {}
                        )
                        if info:
                            block.append("  血氧：" + _kv(info))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"spo2 {iso}: {exc}")

                if "daily" in wanted:
                    try:
                        info = _pick_daily(
                            await self._get_json(
                                "/usersummary-service/v2/dailysummaries"
                                f"?startDate={iso}&endDate={iso}"
                            )
                        )
                        if info:
                            block.append("  每日汇总：" + _kv(info))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"daily {iso}: {exc}")

                if len(block) > 1:
                    lines.append("\n".join(block))

            if len(lines) == 1:
                hint = "（接口报错：" + "；".join(errors[:3]) + "）" if errors else ""
                return f"{start} 至 {end} 没有取到所选指标的数据{hint}"
            if errors:
                lines.append("部分指标未取到：" + "；".join(errors[:5]))
            return _clip("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    @filter.llm_tool(name="garmin_daily_summary")
    async def garmin_daily_summary(
        self,
        event: AstrMessageEvent,
        start_date: str = "",
        end_date: str = "",
    ):
        '''查询每日活动与消耗汇总：步数、步数目标、活动热量、静息心率、中/高强度分钟。

        Args:
            start_date(string): 开始日期，格式 YYYY-MM-DD；留空表示结束日期当天
            end_date(string): 结束日期，格式 YYYY-MM-DD；留空表示今天；最大跨度 14 天
        '''
        try:
            end = _parse_date(end_date)
            start = _parse_date(start_date, default=end) if start_date else end
            days = _daterange(start, end)
            if len(days) > 14:
                days = days[-14:]
                start = days[0]
            lines = [f"{start} 至 {end} 每日汇总："]
            for d in days:
                iso = d.isoformat()
                info = _pick_daily(
                    await self._get_json(
                        "/usersummary-service/v2/dailysummaries"
                        f"?startDate={iso}&endDate={iso}"
                    )
                )
                if info:
                    lines.append(f"  {iso}  " + _kv(info))
            if len(lines) == 1:
                return f"{start} 至 {end} 没有取到每日汇总数据。"
            return _clip("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    @filter.llm_tool(name="garmin_fitness_age")
    async def garmin_fitness_age(self, event: AstrMessageEvent, date_str: str = ""):
        '''查询健身年龄（Fitness Age）及其构成要素。

        Args:
            date_str(string): 日期，格式 YYYY-MM-DD；留空表示今天
        '''
        try:
            day = _parse_date(date_str)
            data = await self._get_json(
                f"/fitnessage-service/fitnessage/v2/{day.isoformat()}"
            )
            if not data:
                return f"{day} 没有健身年龄数据。"
            out = {
                "健身年龄 ": data.get("fitnessAge"),
                "实际年龄 ": data.get("chronologicalAge"),
                "上次健身年龄 ": data.get("previousFitnessAge"),
            }
            components = data.get("components") or {}
            lines = [f"{day} 健身年龄：" + _kv(out)]
            for key, comp in components.items():
                if not isinstance(comp, dict):
                    continue
                value = comp.get("value")
                if value is None:
                    continue
                target = comp.get("targetValue")
                lines.append(
                    f"  {key}：当前 {value}" + (f" / 目标 {target}" if target else "")
                )
            return _clip("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    @filter.llm_tool(name="garmin_profile")
    async def garmin_profile(self, event: AstrMessageEvent):
        '''查询当前佳明账号的基本资料与偏好设置，可用于确认账号区域与 Key 是否有效。'''
        try:
            data = await self._get_json("/userprofile-service/userprofile/settings")
            if not data:
                return "未获取到账号资料。"
            keep = {
                k: data.get(k)
                for k in (
                    "displayName",
                    "fullName",
                    "preferredLocale",
                    "measurementSystem",
                    "firstDayOfWeek",
                    "defaultWeightUnit",
                )
                if data.get(k) is not None
            }
            return "账号资料：" + json.dumps(keep, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    @filter.llm_tool(name="garmin_pace_hr_analysis")
    async def garmin_pace_hr_analysis(
        self,
        event: AstrMessageEvent,
        start_date: str = "",
        end_date: str = "",
        activity_type: str = "running",
    ):
        '''分析指定日期范围内跑步/骑行的配速与心率区间分布，给出训练强度结构与趋势。

        Args:
            start_date(string): 开始日期，格式 YYYY-MM-DD；留空表示从 end_date 往前 30 天
            end_date(string): 结束日期，格式 YYYY-MM-DD；留空表示今天
            activity_type(string): 活动类型，默认 running，可选 cycling、walking 等
        '''
        try:
            end = _parse_date(end_date)
            start = (
                _parse_date(start_date, default=end - timedelta(days=30))
                if start_date
                else end - timedelta(days=30)
            )
            data = await self._get_json(
                "/activitylist-service/activities/search/activities"
                f"?start=0&limit=50"
                f"&startDate={start.isoformat()}&endDate={end.isoformat()}"
            )
            acts = data if isinstance(data, list) else (data or {}).get("activityList")
            acts = [a for a in (acts or []) if isinstance(a, dict)]
            want = (activity_type or "running").strip().lower()
            acts = [
                a
                for a in acts
                if ((a.get("activityType") or {}).get("typeKey") or "").lower() == want
            ]
            if not acts:
                return f"{start} 至 {end} 没有 {want} 类型的活动记录。"

            total_dist = sum(a.get("distance") or 0 for a in acts)
            total_dur = sum(a.get("duration") or 0 for a in acts)
            total_load = sum(a.get("activityTrainingLoad") or 0 for a in acts)
            avg_speed = (total_dist / total_dur) if total_dur else 0

            # 心率区间（秒）
            zones = [0.0] * 5
            for a in acts:
                for i in range(1, 6):
                    zones[i - 1] += float(a.get(f"hrTimeInZone_{i}") or 0)
            zone_total = sum(zones)

            # 配速分布（按 km 配速分桶）
            buckets = [
                ("<4'30\"", 0, 270),
                ("4'30-5'00\"", 270, 300),
                ("5'00-5'30\"", 300, 330),
                ("5'30-6'00\"", 330, 360),
                (">6'00\"", 360, 10**9),
            ]
            pace_count = {label: 0 for label, _, _ in buckets}
            pace_dist = {label: 0.0 for label, _, _ in buckets}
            for a in acts:
                sp = a.get("averageSpeed") or 0
                if sp <= 0:
                    continue
                sec = 1000.0 / sp
                for label, lo, hi in buckets:
                    if lo <= sec < hi:
                        pace_count[label] += 1
                        pace_dist[label] += (a.get("distance") or 0) / 1000.0
                        break

            lines = [
                f"{start} 至 {end} {want} 分析（{len(acts)} 次）：",
                "  总距离 %.2f km，总时长 %s，累计训练负荷 %d"
                % (total_dist / 1000.0, _fmt_duration(total_dur), round(total_load)),
                f"  整体平均配速 {_fmt_pace(avg_speed)}",
            ]

            # 排除低于 1 km 的碎片记录（手动计圈或误触），否则配速会被极短距离拉偏
            candidates = [
                a
                for a in acts
                if (a.get("averageSpeed") or 0) > 0
                and (a.get("distance") or 0) >= 1000
            ]
            fastest = min(
                candidates,
                key=lambda a: -(a.get("averageSpeed") or 0),
                default=None,
            )
            longest = max(acts, key=lambda a: a.get("distance") or 0)
            if fastest:
                lines.append(
                    "  最快一次 %s %s（%s）"
                    % (
                        str(fastest.get("startTimeLocal") or "")[:10],
                        _fmt_km(fastest.get("distance")),
                        _fmt_pace(fastest.get("averageSpeed")),
                    )
                )
            lines.append(
                "  最长一次 %s %s"
                % (
                    str(longest.get("startTimeLocal") or "")[:10],
                    _fmt_km(longest.get("distance")),
                )
            )

            if zone_total > 0:
                lines.append("  心率区间分布（按时间）：")
                for i, sec in enumerate(zones, start=1):
                    pct = sec / zone_total * 100
                    bar = "█" * max(1, round(pct / 5)) if sec else ""
                    lines.append(
                        f"    Z{i}（{HR_ZONE_NAMES[i]}）：{_fmt_duration(sec)}"
                        f"  占 {pct:.0f}%  {bar}"
                    )

            lines.append("  配速分布：")
            for label, _, _ in buckets:
                if pace_count[label]:
                    lines.append(
                        f"    {label}：{pace_count[label]} 次，共 {pace_dist[label]:.1f} km"
                    )

            # 按周汇总里程
            weekly: dict[str, float] = {}
            for a in acts:
                raw = str(a.get("startTimeLocal") or "")[:10]
                if len(raw) != 10:
                    continue
                try:
                    d = datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    continue
                iso = d.isocalendar()
                key = f"{iso[0]}-W{iso[1]:02d}"
                weekly[key] = weekly.get(key, 0.0) + (a.get("distance") or 0) / 1000.0
            if weekly:
                lines.append("  每周里程：")
                for week in sorted(weekly)[-6:]:
                    lines.append(f"    {week}：{weekly[week]:.1f} km")

            return _clip("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    @filter.llm_tool(name="garmin_week_report")
    async def garmin_week_report(self, event: AstrMessageEvent, end_date: str = ""):
        '''生成最近 7 天的训练与恢复周报：跑量、配速、训练负荷、睡眠、HRV、静息心率、步数。

        Args:
            end_date(string): 周报截止日期，格式 YYYY-MM-DD；留空表示今天（含当天）
        '''
        try:
            end = _parse_date(end_date)
            start = end - timedelta(days=6)
            s, e = start.isoformat(), end.isoformat()

            async def safe(coro):
                try:
                    return await coro
                except Exception:  # noqa: BLE001 - 周报允许部分指标缺失
                    return None

            acts_task = self._get_json(
                "/activitylist-service/activities/search/activities"
                f"?start=0&limit=50&startDate={s}&endDate={e}"
            )
            sleep_task = self._get_json(
                "/sleep-service/sleep/dailySleepSummariesByDate"
                f"?startDate={s}&endDate={e}"
            )
            hrv_task = self._get_json(f"/hrv-service/hrv/daily/{s}/{e}")
            daily_task = self._get_json(
                f"/usersummary-service/v2/dailysummaries?startDate={s}&endDate={e}"
            )
            hr_tasks = [
                safe(self._get_json(f"/wellness-service/wellness/dailyHeartRate?date={d.isoformat()}"))
                for d in _daterange(start, end)
            ]
            acts, sleeps, hrvs, dailies = await asyncio.gather(
                safe(acts_task), safe(sleep_task), safe(hrv_task), safe(daily_task)
            )
            hrs = await asyncio.gather(*hr_tasks)

            acts = [a for a in (acts or []) if isinstance(a, dict)] if isinstance(acts, list) else []
            report: list[str] = [f"{s} ~ {e} 佳明周报"]

            # ---- 训练
            runs = [
                a
                for a in acts
                if ((a.get("activityType") or {}).get("typeKey") or "") == "running"
            ]
            total_km = sum(a.get("distance") or 0 for a in acts) / 1000.0
            total_sec = sum(a.get("duration") or 0 for a in acts)
            total_load = sum(a.get("activityTrainingLoad") or 0 for a in acts)
            if acts:
                report.append(
                    f"训练：{len(acts)} 次（跑步 {len(runs)} 次），"
                    f"共 {total_km:.1f} km，{_fmt_duration(total_sec)}，"
                    f"训练负荷合计 {round(total_load)}"
                )
                if runs:
                    run_dist = sum(a.get("distance") or 0 for a in runs)
                    run_dur = sum(a.get("duration") or 0 for a in runs)
                    report.append(
                        f"跑步：{len(runs)} 次 / {run_dist / 1000:.1f} km，"
                        f"平均配速 {_fmt_pace(run_dist / run_dur if run_dur else 0)}"
                    )
                    longest = max(runs, key=lambda a: a.get("distance") or 0)
                    report.append(
                        f"最长一次：{str(longest.get('startTimeLocal') or '')[:10]} "
                        f"{_fmt_km(longest.get('distance'))}"
                    )
                te = [a.get("aerobicTrainingEffect") for a in acts if a.get("aerobicTrainingEffect")]
                if te:
                    report.append(
                        f"平均有氧训练效果：{sum(te) / len(te):.1f}"
                        f"（最高 {max(te):.1f}）"
                    )
            else:
                report.append("训练：本周没有活动记录")

            # ---- 睡眠
            sleep_rows = sleeps if isinstance(sleeps, list) else []
            scores = []
            sleep_secs = []
            for row in sleep_rows:
                if not isinstance(row, dict):
                    continue
                sc = row.get("sleepScores")
                if isinstance(sc, dict):
                    ov = sc.get("overall")
                    v = ov.get("value") if isinstance(ov, dict) else ov
                    if isinstance(v, (int, float)):
                        scores.append(v)
                if isinstance(row.get("sleepTimeSeconds"), (int, float)):
                    sleep_secs.append(row["sleepTimeSeconds"])
            if scores or sleep_secs:
                parts = []
                if scores:
                    parts.append(f"平均评分 {sum(scores) / len(scores):.0f}")
                if sleep_secs:
                    parts.append(f"平均时长 {_fmt_duration(sum(sleep_secs) / len(sleep_secs))}")
                report.append(f"睡眠：{'，'.join(parts)}（{len(sleep_rows)} 天有数据）")

            # ---- HRV
            hrv_rows = (hrvs or {}).get("hrvSummaries") if isinstance(hrvs, dict) else None
            hrv_vals = []
            hrv_status = {}
            for row in hrv_rows or []:
                if not isinstance(row, dict):
                    continue
                if isinstance(row.get("lastNightAvg"), (int, float)):
                    hrv_vals.append(row["lastNightAvg"])
                st = row.get("status")
                if st:
                    hrv_status[st] = hrv_status.get(st, 0) + 1
            if hrv_vals:
                status_txt = "，".join(
                    f"{HRV_STATUS_CN.get(k, k)} {v} 天" for k, v in hrv_status.items()
                )
                report.append(
                    f"HRV：夜间均值 {sum(hrv_vals) / len(hrv_vals):.0f}"
                    + (f"（{status_txt}）" if status_txt else "")
                )

            # ---- 静息心率
            resting = []
            for hr in hrs:
                if isinstance(hr, dict) and isinstance(hr.get("restingHeartRate"), (int, float)):
                    resting.append(hr["restingHeartRate"])
            if resting:
                report.append(
                    f"静息心率：均值 {sum(resting) / len(resting):.0f} bpm"
                    f"（最低 {min(resting)}，最高 {max(resting)}）"
                )

            # ---- 步数
            rows = (dailies or {}).get("data") if isinstance(dailies, dict) else None
            steps = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                v = ((row.get("movement") or {}).get("steps") or {}).get("value")
                if isinstance(v, (int, float)) and v > 0:
                    steps.append(v)
            if steps:
                report.append(
                    f"步数：日均 {sum(steps) / len(steps):.0f} 步（{len(steps)} 天有数据）"
                )

            # ---- 训练负荷结构提示
            if acts:
                high = sum(
                    1 for a in acts if (a.get("aerobicTrainingEffect") or 0) >= 4.0
                )
                report.append(
                    f"强度结构：高强度课 {high} 次 / 共 {len(acts)} 次"
                    + ("（高强度占比偏高，注意恢复）" if high * 2 > len(acts) else "")
                )

            return _clip("\n".join(report))
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    # ------------------------------------------------------------ 定时推送
    def _push_enabled(self) -> bool:
        cfg = self.config or {}
        return bool(cfg.get("push_enabled")) and bool(
            str(cfg.get("push_umo") or "").strip()
        )

    async def _push_loop(self) -> None:
        """每 30 秒检查一次，到点就主动发送每日/每周推送。

        不依赖 LLM：直接查询数据后调用 context.send_message 发送，
        避免定时任务在模型侧失败导致推送丢失。
        """
        sent_daily = ""
        sent_weekly = ""
        while True:
            try:
                cfg = self.config or {}
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                hhmm = now.strftime("%H:%M")
                umo = str(cfg.get("push_umo") or "").strip()

                if umo and cfg.get("push_enabled"):
                    daily_at = str(cfg.get("push_time") or "08:00").strip()
                    if hhmm == daily_at and sent_daily != today:
                        text = await self._build_daily_push()
                        if text and await self._send_push(umo, text):
                            sent_daily = today
                            logger.info("[garmin] 每日推送已发送")

                if umo and cfg.get("weekly_enabled"):
                    weekly_at = str(cfg.get("weekly_time") or "08:30").strip()
                    try:
                        weekday = int(cfg.get("weekly_weekday") or 1)
                    except (TypeError, ValueError):
                        weekday = 1
                    if (
                        now.isoweekday() == weekday
                        and hhmm == weekly_at
                        and sent_weekly != today
                    ):
                        text = await self.garmin_week_report(None, end_date="")
                        if text and await self._send_push(umo, text):
                            sent_weekly = today
                            logger.info("[garmin] 周报已发送")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 循环不能因单次失败退出
                logger.warning(f"[garmin] 推送循环异常：{exc}")
            await asyncio.sleep(30)

    async def _send_push(self, umo: str, text: str) -> bool:
        """通过 AstrBot 平台适配器主动发送一条文本消息。"""
        if not text:
            return False
        try:
            chain = MessageChain().message(text)
            ok = await self.context.send_message(umo, chain)
            if not ok:
                logger.warning(f"[garmin] 未找到会话对应的平台：{umo}")
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[garmin] 发送推送失败：{exc}")
            return False

    async def _build_daily_push(self) -> str:
        """生成每日推送文本：昨天训练 + 今晨恢复 + 建议。"""
        today = _today()
        yesterday = today - timedelta(days=1)
        lines = [f"佳明每日推送 · {yesterday.month}月{yesterday.day}日"]

        # 昨天训练
        act_text = "休息日，没有训练记录"
        load = 0.0
        try:
            acts = await self._get_json(
                "/activitylist-service/activities/search/activities"
                f"?start=0&limit=10&startDate={yesterday.isoformat()}"
                f"&endDate={yesterday.isoformat()}"
            )
            acts = [a for a in (acts or []) if isinstance(a, dict)]
            if acts:
                load = sum(a.get("activityTrainingLoad") or 0 for a in acts)
                parts = []
                for a in acts[:3]:
                    tkey = (a.get("activityType") or {}).get("typeKey") or "活动"
                    parts.append(
                        f"{tkey} {_fmt_km(a.get('distance'))}"
                        f"@{_fmt_pace(a.get('averageSpeed'))}"
                        + (
                            f" 心率{round(a['averageHR'])}"
                            if a.get("averageHR")
                            else ""
                        )
                    )
                act_text = "、".join(parts)
        except Exception as exc:  # noqa: BLE001
            act_text = f"训练数据获取失败（{exc}）"

        lines.append(f"昨天：{act_text}")

        # 今晨恢复
        sleep_score = None
        sleep_sec = None
        hrv_avg = None
        hrv_status = None
        rhr = None
        try:
            sleep = await self._get_json(
                f"/sleep-service/sleep/dailySleep?date={today.isoformat()}"
            ) or {}
            scores = sleep.get("sleepScores")
            if isinstance(scores, dict):
                overall = scores.get("overall")
                v = overall.get("value") if isinstance(overall, dict) else overall
                if isinstance(v, (int, float)):
                    sleep_score = v
            if isinstance(sleep.get("sleepTimeSeconds"), (int, float)):
                sleep_sec = sleep["sleepTimeSeconds"]
        except Exception:  # noqa: BLE001
            pass
        try:
            hrv = await self._get_json(f"/hrv-service/hrv/{today.isoformat()}") or {}
            summ = hrv.get("hrvSummary") or {}
            hrv_avg = summ.get("lastNightAvg")
            hrv_status = summ.get("status")
        except Exception:  # noqa: BLE001
            pass
        try:
            hr = await self._get_json(
                f"/wellness-service/wellness/dailyHeartRate?date={today.isoformat()}"
            ) or {}
            rhr = hr.get("restingHeartRate")
        except Exception:  # noqa: BLE001
            pass

        bits = []
        if sleep_score is not None:
            bits.append(f"睡眠 {sleep_score} 分")
        if sleep_sec:
            bits.append(_fmt_duration(sleep_sec))
        if hrv_avg is not None:
            bits.append(
                f"HRV {hrv_avg}" + (f"（{HRV_STATUS_CN.get(hrv_status, hrv_status)}）" if hrv_status else "")
            )
        if rhr is not None:
            bits.append(f"静息心率 {rhr}")
        if bits:
            lines.append("今晨：" + " / ".join(bits))

        # 训练建议（简单规则，不调用模型）
        advice = "今天可以从容安排一次轻松跑，注意补水。"
        bad_hrv = hrv_status in ("UNBALANCED", "LOW", "POOR")
        if sleep_score is not None and sleep_score < 70:
            advice = "睡眠偏少，建议今天轻松跑或直接休息，别硬上强度。"
        elif bad_hrv:
            advice = f"HRV 状态{HRV_STATUS_CN.get(hrv_status, hrv_status)}，建议降低强度，以有氧慢跑为主。"
        elif load >= 300:
            advice = "昨天负荷较高，今天建议轻松恢复跑。"
        elif (
            sleep_score is not None
            and sleep_score >= 80
            and hrv_status == "BALANCED"
        ):
            advice = "恢复状态不错，可以安排质量课（节奏跑或间歇）。"
        lines.append("建议：" + advice)
        return "\n".join(lines)
