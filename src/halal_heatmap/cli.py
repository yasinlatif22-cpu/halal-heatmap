"""Command line entry point: screen tickers and print their audit records."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date

from halal_heatmap.config import DENOMINATORS, RATIOS, ConfigError, load_config
from halal_heatmap.facts import UsedFact
from halal_heatmap.overrides import load_overrides
from halal_heatmap.runner import ForwardOnlyError, run_screen, supersede_run, update
from halal_heatmap.sources import SourceError
from halal_heatmap.sources.edgar import EdgarClient
from halal_heatmap.sources.prices import YFinancePrices
from halal_heatmap.sources.wikipedia import fetch_constituents
from halal_heatmap.store import Store


def _money(value) -> str:
    return "n/a" if value is None else f"${value / 1e9:,.3f}bn"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def format_record(record: dict, used: list[UsedFact]) -> str:
    driving = record["driving_denominator"]
    lines = [
        f"{record['ticker']}  {record['screen_date']}  status: {record['status']}",
        f"  reason:   {record['reason']}",
        f"  filing:   {record['filing_form']} {record['filing_accession']} filed {record['filing_date']}, "
        f"period end {record['period_end']}",
        f"  business: {record['business_result']} ({record['business_rule']})",
        f"  inputs:   debt {_money(record['debt'])}"
        + (" [assumed zero]" if record["debt_assumed_zero"] else "")
        + f" | cash+securities {_money(record['cash_and_securities'])}"
        f" | impure income {_money(record['impure_income'])} | revenue {_money(record['revenue'])}",
        f"            interest expense {_money(record['interest_expense'])}"
        f" | total liabilities {_money(record['total_liabilities'])}",
    ]
    if record["financing_receivables_share"] is not None:
        lines.append(
            f"            financing receivables {_money(record['financing_receivables'])}, "
            f"{_pct(record['financing_receivables_share'])} of total assets"
        )
    if record["spot_divergence"] is not None:
        lines.append(f"            spot vs averaged market cap: up to {record['spot_divergence']:.2f}x apart")
    for key in ("debt_note", "debt_check_note", "cash_note", "share_count_note", "post_balance_sheet_event"):
        if record[key]:
            lines.append(f"            {record[key]}")
    if record["debt_implied_rate"] is not None:
        lines.append(f"            interest expense / debt: {_pct(record['debt_implied_rate'])}")
    if record["interest_income_basis"] == "upper_bound_no_disclosure":
        lines.append(
            "            NO INTEREST INCOME DISCLOSED: impure income is an upper bound, "
            f"{record['interest_income_bound_base']} x "
            f"{_pct(record['interest_income_yield_ceiling'])} = {_money(record['interest_income_upper_bound'])}"
        )
    elif record["interest_income_tags"]:
        lines.append(f"            interest income tags: {record['interest_income_tags']}")
    if record["interest_income_basis"] == "net_investment_income":
        lines.append(
            "            impure income is net investment income; it supports a pass only below "
            f"{_pct(record['interest_income_max_ratio'])} of revenue"
        )
    elif record["interest_income_dimensional"]:
        lines.append(
            "            interest income is summed over dimension members; it supports a pass only below "
            f"{_pct(record['interest_income_max_ratio'])} of revenue"
        )
    if record["interest_income_source"] == "filing_xbrl":
        lines.append("            interest income was read from the filing's own XBRL, not companyfacts")
    if record["share_classes"]:
        classes = ", ".join(
            f"{c['class'] or 'all'} {c['shares']:,.0f} @ {c['priced_as']}" + ("" if c["listed"] else " (unlisted)")
            for c in json.loads(record["share_classes"])
        )
        lines.append(f"            share classes: {classes}")
        if record["unlisted_class_share"]:
            lines.append(f"            unlisted classes are {_pct(record['unlisted_class_share'])} of market cap")
    if record["interest_income_annual"]:
        lines.append(
            f"            interest income is the annual figure to {record['interest_income_period_end']}, "
            f"filed {record['interest_income_filing_date']} ({record['interest_income_accession']})"
        )
    lines.append("  by denominator:")
    for name in DENOMINATORS:
        marker = " <- drives verdict" if name == driving else ""
        lines.append(
            f"    {name:8} mcap {_money(record[f'mcap_{name}']):>14} ({record[f'mcap_{name}_obs']} obs)"
            f"  debt {_pct(record[f'debt_ratio_{name}']):>7}  cash {_pct(record[f'cash_ratio_{name}']):>7}"
            f"  -> {record[f'status_{name}'] or 'not available'}{marker}"
        )
    lines.append(f"    impure income / revenue: {_pct(record['impure_ratio'])}")
    lines.append(f"  headroom ({driving}):")
    for name in RATIOS:
        near = "  NEAR THRESHOLD" if record[f"{name}_near"] else ""
        lines.append(
            f"    {name:14} {record[f'{name}_operator']} {_pct(record[f'{name}_threshold'])}"
            f"  headroom {_pct(record[f'{name}_headroom'])} points"
            f" ({_pct(record[f'{name}_headroom_rel'])} of limit){near}"
        )
    flags = [
        label
        for label, on in (
            ("denominators disagree", record["denominator_disagreement"]),
            ("near threshold", record["near_threshold"]),
            ("annual interest income", record["interest_income_annual"]),
            ("net investment income", record["interest_income_basis"] == "net_investment_income"),
            ("interest income upper bound", record["interest_income_basis"] == "upper_bound_no_disclosure"),
            ("spot diverges from average", record["spot_diverges"]),
            ("balance sheet out of date", record["stale_balance_sheet"]),
            ("share count rejected", record["share_counts_rejected"]),
            ("share count jump, review", record["share_count_jump"]),
            ("dimensional interest income", record["interest_income_dimensional"]),
            ("unlisted share classes", record["unlisted_class_share"]),
            ("investments assumed zero", record["investments_assumed_zero"]),
            ("debt implausible", record["debt_implausible"]),
        )
        if on
    ]
    lines.append(f"  flags:    {', '.join(flags) or 'none'}    config {record['config_hash']}")
    notes = json.loads(record["input_notes"] or "{}")
    for key, note in notes.items():
        lines.append(f"  note:     {key}: {note}")
    if used:
        lines.append("  facts used:")
        for u in used:
            period = f"{u.fact.start}..{u.fact.end}" if u.fact.start else f"{u.fact.end}"
            lines.append(
                f"    {u.input}/{u.role}: {u.fact.tag} = {u.fact.value:,.0f} [{period}] "
                f"{u.fact.form} {u.fact.accession} filed {u.fact.filed}"
                + (f" [{u.fact.dims}]" if u.fact.dims else "")
            )
    return "\n".join(lines)


def _progress(args, total: int):
    """Print each result as it is screened, in the form the flags ask for."""
    position = 0
    of = f"/{total}" if total else ""

    def show(ticker, result, record, used, outcome) -> None:
        nonlocal position
        position += 1
        if result is None:
            print(f"[{position}{of}] {ticker} ERROR {outcome}", file=sys.stderr)
        elif args.quiet:
            print(f"[{position}{of}] {ticker} {result.status} ({outcome})", file=sys.stderr)
        elif not args.json:
            print(format_record(record, used if args.facts else []))
            print()
        if record is not None:
            show.records.append(record)

    show.records = []
    return show


def format_change(change) -> str:
    lines = [
        f"{change['screen_date']}  {change['ticker']}  {change['old_status']} -> {change['new_status']}"
        f"  cause: {change['cause']} ({change['cause_detail']})",
        f"    was ({change['previous_screen_date']}): {change['old_reason']}",
        f"    now: {change['new_reason']}",
    ]
    for crossing in json.loads(change["crossings"]):
        before, after = crossing["before"], crossing["after"]
        lines.append(
            f"    {crossing['ratio']} / {after['denominator']} {crossing['direction']} its limit: "
            f"{_pct(before['ratio'])} (limit {before['operator']} {_pct(before['threshold'])}) -> "
            f"{_pct(after['ratio'])} (limit {after['operator']} {_pct(after['threshold'])})"
        )
    others = [f for f in json.loads(change["factors"]) if f["cause"] != change["cause"]]
    if others:
        lines.append("    also changed: " + "; ".join(f"{f['cause']} ({f['detail']})" for f in others))
    return "\n".join(lines)


def _report(args, summary, records: list[dict]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(records, indent=2))
        return
    for event in summary.index_events:
        print(f"index: {event['ticker']} {event['kind']} ({event['name']})")
    for change in summary.changes:
        print(format_change(change))
    print("summary: " + (", ".join(f"{status} {n}" for status, n in sorted(summary.counts.items())) or "nothing due"))
    if summary.run_id is not None:
        print(f"run {summary.run_id}: {summary.saved} stored, {summary.unchanged} unchanged for {summary.as_of}")
    elif summary.unchanged:
        print(f"no new rows: {summary.unchanged} already stored for {summary.as_of}")


def _screen(args) -> int:
    cfg = load_config(args.config)
    overrides = load_overrides(args.overrides)
    as_of = date.fromisoformat(args.date) if args.date else date.today()
    edgar = EdgarClient(cfg.edgar)
    constituents = fetch_constituents(cfg.constituents)
    known = {c.ticker for c in constituents}
    wanted = [t.upper() for t in args.tickers]
    unknown = [t for t in wanted if t not in known]
    if unknown:
        print(f"not in the S&P 500 constituent list: {', '.join(unknown)}", file=sys.stderr)
        return 2

    store = None if args.no_store else Store(args.db)
    show = _progress(args, len(wanted or known))
    try:
        summary = run_screen(
            as_of,
            cfg,
            constituents,
            edgar,
            YFinancePrices(),
            overrides,
            store,
            tickers=wanted or None,
            trigger=args.trigger,
            on_result=show,
        )
    except ForwardOnlyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
    _report(args, summary, show.records)
    return 0


def _update(args) -> int:
    cfg = load_config(args.config)
    overrides = load_overrides(args.overrides)
    as_of = date.fromisoformat(args.date) if args.date else date.today()
    edgar = EdgarClient(cfg.edgar)
    constituents = fetch_constituents(cfg.constituents)
    store = Store(args.db)
    args.quiet, args.json, args.facts = True, False, False
    show = _progress(args, 0)
    try:
        summary = update(as_of, cfg, constituents, edgar, YFinancePrices(), overrides, store, on_result=show)
    except ForwardOnlyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    for reason, count in sorted(Counter(summary.due.values()).items()):
        print(f"due: {count} ({reason})")
    for error in summary.errors:
        print(f"source: {error}", file=sys.stderr)
    _report(args, summary, [])
    return 0


def _changes(args) -> int:
    store = Store(args.db)
    try:
        rows = store.status_changes(args.ticker.upper() if args.ticker else None, args.since)
        events = [] if args.ticker else store.index_events()
        events = [e for e in events if not args.since or e["event_date"] >= args.since]
    finally:
        store.close()
    for event in events:
        print(f"{event['event_date']}  index: {event['ticker']} {event['kind']} ({event['name']})")
    for row in rows:
        print(format_change(row))
    print(f"{len(rows)} status changes, {len(events)} index events")
    return 0


def _supersede(args) -> int:
    store = Store(args.db)
    try:
        changes = supersede_run(store, args.run_id, args.reason)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"run {args.run_id} marked as superseded")
    for change in changes:
        print("measured again: " + format_change(change))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="halal-heatmap")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("screen", help="screen tickers (all constituents if none given)")
    run.add_argument("tickers", nargs="*")
    run.add_argument("--date", help="screen date, YYYY-MM-DD (default: today)")
    run.add_argument("--config", default="config.yaml")
    run.add_argument("--overrides", default="overrides.yaml")
    run.add_argument("--db", default="data/screens.db")
    run.add_argument("--no-store", action="store_true", help="do not write to SQLite")
    run.add_argument("--trigger", default="manual")
    run.add_argument("--facts", action="store_true", help="list every XBRL fact used")
    run.add_argument("--quiet", action="store_true", help="progress lines only, no audit records")
    run.add_argument("--json", action="store_true", help="print audit records as JSON")
    run.set_defaults(func=_screen)
    upd = sub.add_parser("update", help="screen what is due: everything monthly, otherwise what has changed")
    upd.add_argument("--date", help="screen date, YYYY-MM-DD (default: today)")
    upd.add_argument("--config", default="config.yaml")
    upd.add_argument("--overrides", default="overrides.yaml")
    upd.add_argument("--db", default="data/screens.db")
    upd.set_defaults(func=_update)
    changes = sub.add_parser("changes", help="list stored status changes and index events")
    changes.add_argument("ticker", nargs="?")
    changes.add_argument("--since", help="only from this screen date, YYYY-MM-DD")
    changes.add_argument("--db", default="data/screens.db")
    changes.set_defaults(func=_changes)
    supersede = sub.add_parser("supersede-run", help="mark a stored run as invalid so nothing uses it")
    supersede.add_argument("run_id", type=int)
    supersede.add_argument("--reason", required=True)
    supersede.add_argument("--db", default="data/screens.db")
    supersede.set_defaults(func=_supersede)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, SourceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
