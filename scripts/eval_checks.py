from __future__ import annotations

"""
Простой оффлайн-eval: гоняем базовые запросы и проверяем числа из БД.
Запуск: python3 scripts/eval_checks.py
"""

from typing import List

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from eniq_agg.bot import (  # noqa: E402
    count_calls_for_day,
    count_calls_last_days,
    daily_counts_last_days,
)
from eniq_agg.db import get_session  # noqa: E402
from eniq_agg.models import Analysis  # noqa: E402


def assert_equal(name: str, lhs, rhs) -> None:
    if lhs != rhs:
        raise AssertionError(f"{name}: expected {rhs}, got {lhs}")


def assert_non_negative(name: str, value: int) -> None:
    if value < 0:
        raise AssertionError(f"{name}: expected non-negative, got {value}")


def test_counts() -> None:
    today = count_calls_for_day(0)
    yesterday = count_calls_for_day(1)
    assert_non_negative("today_count", today)
    assert_non_negative("yesterday_count", yesterday)


def test_last_days_consistency(days: int = 10) -> None:
    total = count_calls_last_days(days)
    per_day: List[dict] = daily_counts_last_days(days)
    sum_per_day = sum(r["calls"] for r in per_day)
    assert_equal(f"last_{days}_days_sum", total, sum_per_day)


def test_has_data_rows() -> None:
    with get_session() as session:
        any_row = session.scalar(session.query(Analysis).limit(1).statement)
        if not any_row:
            raise AssertionError("No Analysis rows found")


def main() -> None:
    test_counts()
    test_last_days_consistency(10)
    test_has_data_rows()
    print("eval_checks: OK")


if __name__ == "__main__":
    main()
