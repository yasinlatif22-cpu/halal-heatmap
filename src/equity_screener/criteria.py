"""Configurable screening criteria: a profile is a named list of metric thresholds.

A profile is data (profiles/*.yaml), not code. Evaluating one against a stock's metrics gives a status per
criterion and an overall status:

  pass               every criterion is met
  fail               at least one criterion is not met
  insufficient_data  no criterion failed but at least one metric is unavailable

The overall status is reported after the stock has cleared the data-quality gates; this module does not
look at them. The web view applies the same rules in JavaScript (web/tool/criteria.js), and a test keeps
the two implementations in agreement on shared cases.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from equity_screener.metrics import METRIC_KEYS

OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
PASS, FAIL, INSUFFICIENT = "pass", "fail", "insufficient_data"


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Criterion:
    metric: str
    op: str
    value: float

    def describe(self) -> str:
        return f"{self.metric} {self.op} {self.value:g}"


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    description: str
    criteria: tuple[Criterion, ...]


@dataclass(frozen=True)
class CriterionResult:
    criterion: Criterion
    actual: float | None
    status: str


@dataclass(frozen=True)
class ProfileResult:
    status: str
    results: tuple[CriterionResult, ...]


def parse_profile(raw: Any, key: str) -> Profile:
    if not isinstance(raw, dict):
        raise ProfileError(f"profile {key!r}: top level must be a mapping")
    criteria = []
    for index, item in enumerate(raw.get("criteria") or []):
        where = f"profile {key!r} criteria[{index}]"
        if not isinstance(item, dict):
            raise ProfileError(f"{where}: expected a mapping")
        metric_key, op, value = item.get("metric"), item.get("op"), item.get("value")
        if metric_key not in METRIC_KEYS:
            raise ProfileError(f"{where}: unknown metric {metric_key!r}; known metrics: {', '.join(METRIC_KEYS)}")
        if op not in OPS:
            raise ProfileError(f"{where}: operator must be one of {', '.join(OPS)}, got {op!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProfileError(f"{where}: value must be a number, got {value!r}")
        criteria.append(Criterion(metric_key, op, float(value)))
    if not criteria:
        raise ProfileError(f"profile {key!r}: needs at least one criterion")
    name = raw.get("name")
    if not name:
        raise ProfileError(f"profile {key!r}: missing name")
    return Profile(key, str(name), str(raw.get("description", "")).strip(), tuple(criteria))


def load_profiles(directory: str | Path) -> dict[str, Profile]:
    profiles = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        with open(path, encoding="utf-8") as handle:
            profiles[path.stem] = parse_profile(yaml.safe_load(handle), path.stem)
    return profiles


def evaluate(profile: Profile, metrics: dict[str, float | None]) -> ProfileResult:
    results = []
    for criterion in profile.criteria:
        actual = metrics.get(criterion.metric)
        if actual is None:
            status = INSUFFICIENT
        else:
            status = PASS if OPS[criterion.op](actual, criterion.value) else FAIL
        results.append(CriterionResult(criterion, actual, status))
    statuses = {r.status for r in results}
    overall = FAIL if FAIL in statuses else INSUFFICIENT if INSUFFICIENT in statuses else PASS
    return ProfileResult(overall, tuple(results))
