"""Tests for verifier tier classification and the V3 service smoke.

Imports verifier directly (not via the package __init__) so the tests don't
depend on Flask being installed in the test environment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VERIFIER_PATH = _HERE.parent / "verifier.py"

_spec = importlib.util.spec_from_file_location("verifier_under_test", _VERIFIER_PATH)
verifier = importlib.util.module_from_spec(_spec)
sys.modules["verifier_under_test"] = verifier
_spec.loader.exec_module(verifier)  # type: ignore[union-attr]


def test_classify_tier_picks_highest():
    assert verifier.classify_tier([]) == verifier.V1_SMOKE
    assert verifier.classify_tier(["README.md"]) == verifier.V1_SMOKE
    assert verifier.classify_tier(["pkg/util.py"]) == verifier.V2_UNIT
    assert verifier.classify_tier(["pkg/routes.py"]) == verifier.V3_SERVICE
    assert verifier.classify_tier(["pkg/static/main.js"]) == verifier.V4_UI
    assert verifier.classify_tier(["migrations/001.sql"]) == verifier.V5_SCHEMA
    # Mixed → highest wins.
    assert verifier.classify_tier(
        ["README.md", "pkg/routes.py", "migrations/x.sql"]
    ) == verifier.V5_SCHEMA


def test_v3_falls_back_to_v2_when_v2_fails(tmp_path: Path):
    # File missing → V2 reports compile failure → V3 must not spawn anything.
    v = verifier._v3_service(tmp_path, ["does/not/exist.py"])
    assert v.tier == verifier.V3_SERVICE
    assert v.status == "fail"
    assert v.blocked_apply is True
    assert v.evidence["service_smoke"] == {"skipped": "V2 failed"}


def test_v3_skips_smoke_when_dashboard_not_in_scope(tmp_path: Path):
    # Real .py file, but no utilization_dashboard files in the diff →
    # smoke is out of scope, V3 should pass through V2's verdict.
    pkg = tmp_path / "live" / "OpenDentalMCP" / "otherpkg"
    pkg.mkdir(parents=True)
    (pkg / "routes.py").write_text("x = 1\n")
    rel = "live/OpenDentalMCP/otherpkg/routes.py"

    v = verifier._v3_service(tmp_path, [rel])
    assert v.tier == verifier.V3_SERVICE
    smoke = v.evidence["service_smoke"]
    assert smoke["touches_dashboard"] is False
    assert smoke["standalone_present"] is False
    # V2 succeeded (compile ok, no tests) → V3 inherits pass.
    assert v.status == "pass"
    assert v.blocked_apply is False


def test_v3_smoke_records_failure_when_runner_crashes(tmp_path: Path, monkeypatch):
    # Stand up a fake worktree with a "standalone" module that exits rc=1
    # immediately. Verifier must capture stdout_tail and mark the smoke failed.
    pkg_dir = tmp_path / "live" / "OpenDentalMCP"
    dash = pkg_dir / "utilization_dashboard"
    dash.mkdir(parents=True)
    (dash / "__init__.py").write_text("")
    (dash / "standalone.py").write_text(
        "import sys\nsys.stderr.write('boom\\n')\nsys.exit(1)\n"
    )
    rel = "live/OpenDentalMCP/utilization_dashboard/routes.py"
    (dash / "routes.py").write_text("x = 1\n")

    v = verifier._v3_service(tmp_path, [rel])
    assert v.tier == verifier.V3_SERVICE
    assert v.status == "fail"
    assert v.blocked_apply is True
    smoke = v.evidence["service_smoke"]
    assert "stdout_tail" in smoke
    assert "boom" in smoke["stdout_tail"]
