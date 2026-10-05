from datetime import date, timedelta

import pytest

from conftest import trading_days
from halal_heatmap.config import TagRef
from halal_heatmap.facts import Fact, ShareCount
from halal_heatmap.marketcap import (
    add_months,
    check_share_counts,
    compute_market_caps,
    daily_class_market_caps,
    daily_market_caps,
    split_factor,
)

AS_OF = date(2026, 10, 1)


def count(effective, shares, basis=None):
    return ShareCount(effective=effective, basis=basis or effective, shares=shares, fact=None)


def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert add_months(date(2026, 10, 1), -12) == date(2025, 10, 1)
    assert add_months(date(2026, 1, 15), -36) == date(2023, 1, 15)


def test_split_factor_counts_only_later_splits():
    splits = [(date(2024, 6, 10), 4.0), (date(2025, 6, 10), 2.0)]
    assert split_factor(splits, date(2024, 1, 1)) == 8.0
    assert split_factor(splits, date(2024, 6, 10)) == 2.0
    assert split_factor(splits, date(2025, 12, 1)) == 1.0


def test_daily_market_cap_steps_with_filed_share_counts():
    closes = [(date(2026, 1, 5), 10.0), (date(2026, 2, 5), 10.0), (date(2026, 3, 5), 12.0)]
    shares = [count(date(2026, 2, 1), 100), count(date(2026, 3, 1), 90)]
    # No share count is public on the first day, so it produces no observation.
    assert daily_market_caps(closes, [], shares) == [(date(2026, 2, 5), 1000.0), (date(2026, 3, 5), 1080.0)]


def test_market_cap_is_unchanged_across_a_split():
    # 2-for-1 split on 10 March: adjusted closes are continuous, the pre-split count is doubled.
    closes = [(date(2026, 3, 5), 50.0), (date(2026, 3, 12), 50.0), (date(2026, 5, 5), 50.0)]
    splits = [(date(2026, 3, 10), 2.0)]
    shares = [count(date(2026, 2, 1), 100), count(date(2026, 5, 1), 200)]
    assert [value for _, value in daily_market_caps(closes, splits, shares)] == [10000.0, 10000.0, 10000.0]


def test_spot_and_averages(cfg):
    days = trading_days(date(2023, 9, 1), AS_OF)
    cutoff = add_months(AS_OF, -12)
    daily = [(day, 200.0 if day > cutoff else 100.0) for day in days]
    caps = compute_market_caps(daily, AS_OF, cfg.market_cap)
    assert caps["spot"].value == 200.0 and caps["spot"].end == AS_OF
    assert caps["avg_12m"].value == pytest.approx(200.0)
    in_window = [value for day, value in daily if day > add_months(AS_OF, -36)]
    assert caps["avg_36m"].value == pytest.approx(sum(in_window) / len(in_window))
    assert caps["avg_36m"].observations == len(in_window)
    assert 100.0 < caps["avg_36m"].value < 200.0


def test_observations_after_the_screen_date_are_ignored(cfg):
    days = trading_days(date(2025, 9, 1), AS_OF + timedelta(days=30))
    daily = [(day, 999.0 if day > AS_OF else 100.0) for day in days]
    caps = compute_market_caps(daily, AS_OF, cfg.market_cap)
    assert caps["spot"].value == 100.0 and caps["avg_12m"].value == pytest.approx(100.0)


def test_short_history_gives_no_average(cfg):
    daily = [(day, 100.0) for day in trading_days(date(2025, 6, 1), AS_OF)]  # 16 months
    caps = compute_market_caps(daily, AS_OF, cfg.market_cap)
    assert caps["avg_12m"].value == pytest.approx(100.0)
    assert caps["avg_36m"].value is None and "needed" in caps["avg_36m"].note

    recent = [(day, 100.0) for day in trading_days(date(2026, 3, 1), AS_OF)]  # 7 months
    assert compute_market_caps(recent, AS_OF, cfg.market_cap)["avg_12m"].value is None


def test_stale_or_absent_prices_give_no_spot(cfg):
    daily = [(day, 100.0) for day in trading_days(date(2025, 6, 1), AS_OF - timedelta(days=10))]
    assert compute_market_caps(daily, AS_OF, cfg.market_cap)["spot"].value is None
    assert compute_market_caps([], AS_OF, cfg.market_cap)["spot"].value is None


def share_count(shares, filed, accession, symbol="X", basis=None):
    tag = TagRef("dei", "EntityCommonStockSharesOutstanding")
    fact = Fact(tag, "shares", shares, None, filed, accession, "10-Q", filed)
    return ShareCount(filed, basis or filed, shares, fact, symbol)


QUARTERS = [date(2025, 8, 7), date(2025, 11, 6), date(2026, 2, 26), date(2026, 5, 8), date(2026, 8, 7)]


def quarterly(values, **kwargs):
    return [share_count(v, day, f"acc-{i}", **kwargs) for i, (v, day) in enumerate(zip(values, QUARTERS, strict=False))]


def test_mis_scaled_share_count_is_rejected_and_its_neighbours_carry_on(cfg):
    """Packaging Corp: one cover page reported 89 billion shares instead of 89 million."""
    counts = quarterly([89_978_783, 89_977_067, 89_213_394_000, 89_098_647, 89_098_299])
    check = check_share_counts(counts, {"X": []}, cfg.shares.sanity)
    assert [c.shares for c in check.counts] == [89_978_783, 89_977_067, 89_098_647, 89_098_299]
    assert check.rejected == 1 and not check.jump
    assert "rejected share count 89,213,394,000 filed 2026-02-26 (acc-2)" in check.note
    closes = [(date(2026, 3, 2), 200.0), (date(2026, 6, 1), 200.0)]
    daily = daily_class_market_caps({"X": (closes, [])}, check.counts)
    assert daily == [(date(2026, 3, 2), 89_977_067 * 200.0), (date(2026, 6, 1), 89_098_647 * 200.0)]


@pytest.mark.parametrize("position", [0, 4])
def test_mis_scaled_count_at_either_end_is_rejected(cfg, position):
    values = [100.0] * 5
    values[position] = 100_000.0
    check = check_share_counts(quarterly(values), {"X": []}, cfg.shares.sanity)
    assert check.rejected == 1 and [c.shares for c in check.counts] == [100.0] * 4


def test_a_real_split_explains_a_large_change_in_count(cfg):
    counts = quarterly([100.0, 100.0, 1000.0, 1000.0, 1000.0])
    check = check_share_counts(counts, {"X": [(date(2026, 1, 15), 10.0)]}, cfg.shares.sanity)
    assert (check.rejected, check.jump, check.note) == (0, False, "") and len(check.counts) == 5
    # Without the split on record the lasting change is not rejected (either side could be right) but flagged.
    unexplained = check_share_counts(counts, {"X": []}, cfg.shares.sanity)
    assert unexplained.rejected == 0 and unexplained.jump


def test_reject_limit_is_the_configured_multiple(cfg):
    assert check_share_counts(quarterly([100.0, 100.0, 500.0, 100.0, 100.0]), {}, cfg.shares.sanity).rejected == 0
    assert check_share_counts(quarterly([100.0, 100.0, 501.0, 100.0, 100.0]), {}, cfg.shares.sanity).rejected == 1
    assert check_share_counts(quarterly([100.0, 100.0, 19.0, 100.0, 100.0]), {}, cfg.shares.sanity).rejected == 1


def test_halved_share_count_is_flagged_for_review_but_kept(cfg):
    """Honeywell: 634m shares became 317m in one filing."""
    check = check_share_counts(quarterly([634.0, 635.0, 633.0, 317.0]), {}, cfg.shares.sanity)
    assert check.rejected == 0 and check.jump and len(check.counts) == 4
    assert "share count changed 2.00x on a split-adjusted basis" in check.note
    assert "2026-02-26 and 2026-05-08 (reported 633 then 317)" in check.note
    assert not check_share_counts(quarterly([634.0, 635.0, 633.0, 600.0]), {}, cfg.shares.sanity).jump


def test_two_counts_that_disagree_wildly_leave_none(cfg):
    check = check_share_counts(quarterly([100.0, 100_000.0]), {}, cfg.shares.sanity)
    assert check.counts == [] and check.rejected == 2
    single = check_share_counts(quarterly([100.0]), {}, cfg.shares.sanity)
    assert len(single.counts) == 1 and single.rejected == 0  # nothing to compare with


def test_share_classes_are_checked_separately(cfg):
    counts = quarterly([10.0] * 5, symbol="X.A") + quarterly([15000.0] * 5, symbol="X.B")
    check = check_share_counts(counts, {}, cfg.shares.sanity)
    assert check.rejected == 0 and not check.jump and len(check.counts) == 10
