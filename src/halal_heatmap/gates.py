"""Gates a run must pass before its results are published. Pure: each gate reads the facts of a run
(RunSummary.facts()) and the limits in config.yaml's `publish` section, and says why it failed.

A failed gate stops the publish step. Nothing from a failed run is exported or deployed, so a
partial run never reaches the site. The one manual override is for status changes, which can be
real after a methodology change. It does not relax the error, price or constituent gates.
"""

from __future__ import annotations

from collections.abc import Mapping

from halal_heatmap.config import Publish


def check_run(facts: Mapping, publish: Publish, *, accept_status_changes: bool = False) -> list[str]:
    """The reasons this run must not be published. Empty when every gate passes."""
    failures: list[str] = []
    total = facts["constituents"]
    if total <= 0:
        failures.append("no constituents were read")
        return failures

    errors = len(facts["errors"])
    if errors / total > publish.max_error_share:
        failures.append(
            f"{errors} source errors for {total} companies ({errors / total:.1%}), "
            f"above the limit of {publish.max_error_share:.1%}"
        )

    price_failures = facts["source_failures"].get("prices", 0)
    if price_failures > publish.max_price_failures:
        failures.append(
            f"closes could not be read for {price_failures} companies, "
            f"above the limit of {publish.max_price_failures}"
        )

    previous = facts["previous_constituents"]
    if previous is not None and previous - total > publish.max_constituent_drop:
        failures.append(
            f"constituent list fell from {previous} to {total}, "
            f"by more than the limit of {publish.max_constituent_drop}"
        )

    changes = facts["status_changes"]
    if not accept_status_changes and changes / total > publish.max_status_change_share:
        failures.append(
            f"{changes} status changes for {total} companies ({changes / total:.1%}), "
            f"above the limit of {publish.max_status_change_share:.1%}; "
            "review them, then publish with the accept option if they are real"
        )
    return failures


def check_warnings(facts: Mapping, publish: Publish) -> list[str]:
    """Conditions worth a look that do not stop a publish."""
    total = facts["constituents"]
    lagging = len(facts["lagging"])
    if total > 0 and lagging / total > publish.max_lagging_share:
        return [
            f"{lagging} of {total} companies ({lagging / total:.1%}) have a filing companyfacts does not serve "
            f"yet, above the {publish.max_lagging_share:.0%} warning level; check companyfacts"
        ]
    return []


def check_prices(failed: int, publish: Publish) -> list[str]:
    """The daily price change is exported only when every company's closes were read."""
    if failed > publish.max_price_failures:
        return [
            f"daily price change could not be read for {failed} companies, "
            f"above the limit of {publish.max_price_failures}"
        ]
    return []
