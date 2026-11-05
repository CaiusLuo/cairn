"""Host-only parsing of explicit one-shot scheduling requests.

The model never supplies the authoritative time, content or type. This module
turns a trusted inbound message into an immutable request, a clarification, or
a non-scheduling result. It intentionally supports only a small, explicit set
of Chinese time expressions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

_TIMEZONE = "Asia/Shanghai"

_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_RELATIVE = re.compile(
    r"(?P<number>\d+|[零一二两三四五六七八九十]+)\s*(?P<unit>分钟|小时)(?:后|以后)"
)
_ABSOLUTE = re.compile(
    r"(?P<day>今天|明天|后天)?\s*(?P<period>上午|下午|晚上)?\s*"
    r"(?P<hour>\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*(?:点|时)"
    r"(?:\s*(?P<minute>\d{1,2}|[零一二三四五六七八九十]{1,3})\s*分)?"
)
_ISO = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})")
_NEGATIVE_PREFIXES = ("不要", "别", "不用", "无需", "禁止", "不是说", "并非")
_NEGATIVE_MARKERS = ()
_ACTION_PREFIXES = (
    "提醒",
    "检查",
    "查看",
    "查询",
    "运行",
    "执行",
    "测试",
    "部署",
    "发布",
    "发",
    "通知",
    "确认",
    "修复",
    "更新",
    "整理",
    "跑",
    "看",
)


@dataclass(frozen=True, slots=True)
class OnceRequest:
    message_id: str
    received_at: datetime
    at: datetime
    prompt: str
    reminder: bool


@dataclass(frozen=True, slots=True)
class Clarification:
    reason: str


@dataclass(frozen=True, slots=True)
class NotSchedule:
    pass


Result = OnceRequest | Clarification | NotSchedule


def _number(text: str) -> int:
    if text.isdigit():
        return int(text)
    if "十" in text:
        tens, _, ones = text.partition("十")
        return (10 if not tens else _CN_DIGITS[tens] * 10) + (
            _CN_DIGITS.get(ones, 0) if ones else 0
        )
    if len(text) == 1 and text in _CN_DIGITS:
        return _CN_DIGITS[text]
    raise ValueError("unsupported number")


def _received(received_at: datetime | float, timezone: str) -> datetime:
    zone = ZoneInfo(timezone or _TIMEZONE)
    if isinstance(received_at, (int, float)):
        return datetime.fromtimestamp(float(received_at), tz=zone)
    if received_at.tzinfo is None:
        return received_at.replace(tzinfo=zone)
    return received_at.astimezone(zone)


def _explicit_time(
    text: str, received_at: datetime, zone: ZoneInfo
) -> tuple[datetime, tuple[int, int]] | None:
    base = text
    for prefix in ("麻烦", "请"):
        if base.startswith(prefix):
            base = base[len(prefix) :].lstrip()
            break
    span_offset = len(text) - len(base)
    iso = _ISO.match(base)
    if iso is not None:
        local = datetime.fromisoformat(
            f"{iso.group('date')}T{int(iso.group('hour')):02d}:{iso.group('minute')}"
        ).replace(tzinfo=zone)
        start, end = iso.span()
        return local, (start + span_offset, end + span_offset)
    absolute = _ABSOLUTE.match(base)
    if absolute is not None:
        hour = _number(absolute.group("hour"))
        minute = _number(absolute.group("minute")) if absolute.group("minute") else 0
        if absolute.group("period") in {"下午", "晚上"} and hour < 12:
            hour += 12
        day = absolute.group("day") or "今天"
        day_offset = {"今天": 0, "明天": 1, "后天": 2}[day]
        target_date = (received_at + timedelta(days=day_offset)).date()
        start, end = absolute.span()
        return (
            datetime.combine(target_date, dt_time(hour, minute), tzinfo=zone),
            (start + span_offset, end + span_offset),
        )
    relative = _RELATIVE.match(base)
    if relative is not None:
        amount = _number(relative.group("number"))
        delta = (
            timedelta(minutes=amount)
            if relative.group("unit") == "分钟"
            else timedelta(hours=amount)
        )
        start, end = relative.span()
        return received_at + delta, (start + span_offset, end + span_offset)
    return None


def parse_once_request(
    text: str,
    received_at: datetime | float,
    timezone: str = _TIMEZONE,
    *,
    message_id: str = "",
    now: datetime | float | None = None,
) -> Result:
    """Parse one explicit one-shot request from a trusted inbound message."""
    raw = (text or "").strip()
    if not raw:
        return NotSchedule()
    if any(raw.startswith(prefix) for prefix in _NEGATIVE_PREFIXES):
        return NotSchedule()
    if "“" in raw or "”" in raw or (raw.count('"') >= 2):
        return NotSchedule()
    zone = ZoneInfo(timezone or _TIMEZONE)
    received = _received(received_at, timezone)
    occurrences = (
        len(_ISO.findall(raw))
        + len(_RELATIVE.findall(raw))
        + len(_ABSOLUTE.findall(raw))
    )
    if occurrences > 1:
        return Clarification("multiple times are ambiguous")
    try:
        parsed = _explicit_time(raw, received, zone)
    except (ValueError, OverflowError):
        return Clarification("invalid time")
    if parsed is None:
        if any(cue in raw for cue in ("提醒", "定时", "安排", "晚点", "稍后")):
            return Clarification("missing explicit time")
        return NotSchedule()
    at, span = parsed
    actual_now = _received(now, timezone) if now is not None else datetime.now(zone)
    if at <= received or at <= actual_now:
        return Clarification("scheduled time is in the past")
    suffix = raw[span[1] :].strip(" \uff0c,。.!\uff01")
    reminder = False
    if suffix.startswith("提醒我"):
        reminder = True
        suffix = suffix[3:].strip()
    elif suffix.startswith("提醒"):
        reminder = True
        suffix = suffix[2:].strip()
    elif raw.startswith("提醒我"):
        reminder = True
        suffix = raw[3:]
        suffix = suffix[span[0] - 3 : span[1] - 3] + suffix[span[1] - 3 :]
    if not suffix:
        return Clarification("missing reminder or task content")
    if not reminder and not suffix.startswith(_ACTION_PREFIXES):
        return NotSchedule()
    if len(suffix) > 8000:
        suffix = suffix[:8000]
    return OnceRequest(
        message_id=message_id,
        received_at=received,
        at=at,
        prompt=suffix,
        reminder=reminder,
    )
