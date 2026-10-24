"""The browser's criteria rules must agree with the Python ones on the shared cases."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_javascript_matches_the_shared_cases():
    script = (
        "const {evaluate}=require(process.argv[1]);"
        "const cases=JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'));"
        "console.log(JSON.stringify(cases.map(c=>evaluate(c.profile.criteria,c.metrics).status)));"
    )
    out = subprocess.run(
        ["node", "-e", script, str(ROOT / "web/tool/criteria.js"), str(ROOT / "tests/criteria_cases.json")],
        capture_output=True, text=True, check=True,
    ).stdout
    cases = json.loads((ROOT / "tests/criteria_cases.json").read_text())
    assert json.loads(out) == [c["expected"] for c in cases]
