from datetime import date, datetime

from update import _create_event, _parse_event_time


def test_parse_event_time_with_chinese_hour_and_minute():
    assert _parse_event_time("2026-01-02", "3时05分") == datetime(2026, 1, 2, 3, 5)


def test_parse_event_time_with_h_suffix():
    assert _parse_event_time("2026-01-02", "22h") == datetime(2026, 1, 2, 22, 0)


def test_parse_event_time_with_colon():
    assert _parse_event_time("2026-01-02", "18:52") == datetime(2026, 1, 2, 18, 52)


def test_parse_event_time_without_time_returns_date():
    assert _parse_event_time("2026-01-02", "") == date(2026, 1, 2)


def test_event_uid_is_stable_for_same_event():
    start = datetime(2026, 1, 2, 3, 5)
    first_event = _create_event("蜂巢星团合月", start, start, "蜂巢星团在月球以南2.7度")
    second_event = _create_event(
        "蜂巢星团合月", start, start, "蜂巢星团在月球以南2.7度"
    )

    assert first_event["UID"] == second_event["UID"]
