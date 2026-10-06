"""The screen workflow parses, keeps its triggers and token placement, and publishes only stored runs unless
force_publish is set. The conditions are checked by evaluating them, not by matching their text alone."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "screen.yml"
STORED = "steps.gates.outputs.stored == 'true'"
FORCE = "inputs.force_publish"
DEPLOY_STORED = "needs.screen.outputs.stored == 'true'"


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))  # PyYAML reads a bare `on` key as True (YAML 1.1)


def _steps(job: dict) -> dict:
    return {step["name"]: step for step in job["steps"] if "name" in step}


def _runs(condition: str, *, stored: bool, force: bool) -> bool:
    """Evaluate a condition made only of `X == 'true'` terms joined by `||`, as the workflow uses them."""
    result = False
    for term in condition.split(" || "):
        if term in (STORED, DEPLOY_STORED):
            result = result or stored
        elif term == FORCE:
            result = result or force
        else:
            raise AssertionError(f"unexpected term in condition: {term!r}")
    return result


def test_parses_and_triggers_are_unchanged():
    triggers = _triggers(_doc())
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert [entry["cron"] for entry in triggers["schedule"]] == [
        "30 21 * * 1-5",
        "30 13 * 1,2,4,5,7,8,10,11 1-5",
    ]


def test_force_publish_is_a_boolean_input_defaulting_to_false():
    inputs = _triggers(_doc())["workflow_dispatch"]["inputs"]
    assert inputs["force_publish"]["type"] == "boolean"
    assert inputs["force_publish"]["default"] is False


def test_checkout_keeps_no_credentials_and_token_is_only_in_the_push_step():
    doc = _doc()
    job_steps = doc["jobs"]["screen"]["steps"]
    checkout = next(step for step in job_steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
    steps = _steps(doc["jobs"]["screen"])

    text = WORKFLOW.read_text()
    assert text.count("github.token") == 1
    holders = [name for name, step in steps.items() if "GITHUB_TOKEN" in step.get("env", {})]
    assert holders == ["Push the data branch"]


def test_gates_have_no_condition_so_they_always_run():
    steps = _steps(_doc()["jobs"]["screen"])
    assert "if" not in steps["Check the publish gates"]


def test_database_is_only_saved_for_stored_runs():
    steps = _steps(_doc()["jobs"]["screen"])
    assert steps["Commit the database to the data branch"]["if"] == STORED
    assert steps["Push the data branch"]["if"] == STORED


def test_export_and_package_run_on_stored_or_forced_runs():
    steps = _steps(_doc()["jobs"]["screen"])
    for name in ("Export the site data", "Package the site"):
        assert steps[name]["if"] == f"{STORED} || {FORCE}"


def test_deploy_runs_on_stored_or_forced_runs():
    assert _doc()["jobs"]["deploy"]["if"] == f"{DEPLOY_STORED} || {FORCE}"
    assert _doc()["jobs"]["deploy"]["needs"] == "screen"


def test_no_condition_overrides_the_default_success_check():
    # Without always(), failure() or cancelled(), GitHub skips a step once an earlier step has failed. A failed
    # gate therefore still stops the export, the package and the deploy when force_publish is set.
    text = WORKFLOW.read_text()
    for status_function in ("always()", "failure()", "cancelled()"):
        assert status_function not in text


def test_stored_only_behaviour_is_unchanged_without_force():
    for stored in (True, False):
        assert _runs(STORED, stored=stored, force=False) is stored
        assert _runs(f"{STORED} || {FORCE}", stored=stored, force=False) is stored
        assert _runs(DEPLOY_STORED, stored=stored, force=False) is stored
        assert _runs(f"{DEPLOY_STORED} || {FORCE}", stored=stored, force=False) is stored


def test_force_runs_export_package_and_deploy_when_nothing_was_stored():
    assert _runs(f"{STORED} || {FORCE}", stored=False, force=True) is True
    assert _runs(f"{DEPLOY_STORED} || {FORCE}", stored=False, force=True) is True
    assert _runs(STORED, stored=False, force=True) is False  # the save and push stay stored-only
