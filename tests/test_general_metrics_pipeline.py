"""Fundamentals flow from filings to stored results to the exported metrics, with known arithmetic."""


import pytest

from conftest import fact
from equity_screener.export import build_site
from equity_screener.metrics import compute_metrics, figures_from_result
from equity_screener.screen.engine import result_to_record
from equity_screener.store import Store
from test_pipeline import AS_OF, Q2_END, document, duration, run

EXTRA = {
    "NetIncomeLoss": duration((600, 250, 350)),  # trailing 700
    "OperatingIncomeLoss": duration((900, 400, 500)),  # trailing 1,000
    "NetCashProvidedByUsedInOperatingActivities": duration((800, 350, 450)),  # trailing 900
    "PaymentsToAcquirePropertyPlantAndEquipment": duration((200, 90, 110)),  # trailing 220
    "StockholdersEquity": [fact(Q2_END, 3000, "2026-07-30", accn="q2-26")],
}


def test_fundamentals_are_resolved_and_metrics_follow(cfg):
    result, used = run(cfg, document(**EXTRA))  # market cap 10,000; revenue 4,500; debt 2,000
    assert dict(result.inputs.extra)["net_income"] == 700
    m = compute_metrics(figures_from_result(result))
    assert m["net_margin"] == pytest.approx(700 / 4500)
    assert m["roe"] == pytest.approx(700 / 3000)
    assert m["debt_to_equity"] == pytest.approx(2000 / 3000)
    assert m["interest_coverage"] == pytest.approx(1000 / 80)
    assert m["fcf_yield"] == pytest.approx((900 - 220) / 10000)
    assert m["price_to_earnings"] == pytest.approx(10000 / 700)
    assert {"net_income", "stockholders_equity"} <= {u.input for u in used}  # every figure is traceable


def test_missing_fundamentals_do_not_change_the_screen_verdict(cfg):
    with_extra, _ = run(cfg, document(**EXTRA))
    without, _ = run(cfg, document())
    assert with_extra.status == without.status
    assert with_extra.config_hash == without.config_hash
    m = compute_metrics(figures_from_result(without))
    assert m["net_margin"] is None and m["roe"] is None  # unavailable, never estimated
    assert m["debt_to_market_cap"] == pytest.approx(0.2)  # figures the screen already holds still work


def test_metrics_survive_the_store_and_reach_the_export(cfg):
    result, used = run(cfg, document(**EXTRA))
    store = Store(":memory:")
    run_id = store.start_run("2026-10-01", cfg.hash)
    store.save_result(run_id, result_to_record(result, cfg), used)
    store.finish_run(run_id, {}, [])
    site = build_site(store, cfg, {})
    stock = site["screens.json"]["screens"][0]
    assert stock["metrics"]["roe"] == pytest.approx(700 / 3000)
    assert stock["metrics"]["market_cap"] == pytest.approx(10000)
    meta = site["meta.json"]
    assert {m["key"] for m in meta["metric_catalog"]} >= {"roe", "fcf_yield", "price_to_earnings"}
    store.close()


def test_a_result_without_fundamentals_is_screened_again_so_old_data_gains_metrics(cfg):
    """Fundamentals are outside the methodology hash, so the hash cannot trigger this. A stored result with no
    fundamentals (made before the metrics existed) must be due, or old exports would never show metrics."""
    from equity_screener.runner import METRICS_BACKFILL, detect_due
    from test_pipeline import COMPANY, Filings

    old_cfg = cfg
    result, used = run(cfg, document())  # no fundamentals reported: every extra is None
    record = result_to_record(result, cfg)
    record["extra_inputs"] = None  # as a row stored before this feature
    store = Store(":memory:")
    run_id = store.start_run("2026-10-01", cfg.hash)
    store.save_result(run_id, record, used)
    store.finish_run(run_id, {}, [])
    due, _ = detect_due(AS_OF, old_cfg, [COMPANY], Filings(document()), {}, store)
    assert due == {COMPANY.ticker: METRICS_BACKFILL}
    store.close()


def test_the_coverage_script_counts_resolved_and_missing_metrics(cfg):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "verify_fundamentals", Path(__file__).parent.parent / "scripts" / "verify_fundamentals.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    full, _ = run(cfg, document(**EXTRA))
    bare, _ = run(cfg, document())
    records = [result_to_record(full, cfg), result_to_record(bare, cfg)]
    records[1] = {**records[1], "ticker": "BARE"}
    result = module.coverage(records)
    assert result["roe"]["resolved"] == ["TEST"] and result["roe"]["missing"] == ["BARE"]
    assert result["debt_to_market_cap"]["resolved"] == ["TEST", "BARE"]
