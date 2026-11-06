from datetime import datetime
from zoneinfo import ZoneInfo

from cairn.runtime.scheduling.requests import (
    Clarification,
    NotSchedule,
    OnceRequest,
    parse_once_request,
)

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_explicit_relative_reminder() -> None:
    result = parse_once_request(
        "十分钟后提醒我检查部署结果", NOW, message_id="m1", now=NOW
    )
    assert isinstance(result, OnceRequest)
    assert result.at == NOW.replace(minute=10)
    assert result.prompt == "检查部署结果"
    assert result.reminder is True
    assert result.message_id == "m1"


def test_explicit_absolute_execution() -> None:
    result = parse_once_request(
        "明天上午九点检查这个项目的CI\uff0c把结果发给我", NOW, message_id="m2", now=NOW
    )
    assert isinstance(result, OnceRequest)
    assert result.at.date().isoformat() == "2026-11-06"
    assert result.at.hour == 9
    assert result.reminder is False


def test_negated_and_chat_are_not_schedules() -> None:
    assert isinstance(parse_once_request("不要创建任何提醒", NOW), NotSchedule)
    assert isinstance(parse_once_request("请指出这个函数的缺点", NOW), NotSchedule)
    assert isinstance(parse_once_request("你好", NOW), NotSchedule)


def test_ambiguous_or_past_requires_clarification() -> None:
    assert isinstance(parse_once_request("晚点提醒我", NOW), Clarification)
    assert isinstance(parse_once_request("十分钟后提醒我", NOW), Clarification)
    assert isinstance(
        parse_once_request(
            "十分钟后提醒我喝水", NOW.replace(hour=9, minute=20), now=NOW
        ),
        OnceRequest,
    )
    past = parse_once_request("十分钟前提醒我喝水", NOW)
    assert isinstance(past, Clarification)
