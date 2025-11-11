from pathlib import Path

import pytest

from .utils import GoldenScenarioRunner, assert_expected, load_golden_fixture


_SCENARIO_DIR = Path(__file__).with_name("autopilot")
_SCENARIOS = sorted(_SCENARIO_DIR.glob("*.json"))


@pytest.mark.parametrize("scenario_path", _SCENARIOS, ids=lambda p: p.stem)
def test_autopilot_golden(monkeypatch: pytest.MonkeyPatch, scenario_path: Path) -> None:
    scenario = load_golden_fixture(scenario_path)
    expected = scenario.get("expected", {})
    runner = GoldenScenarioRunner(scenario, monkeypatch)
    runner.apply_common_patches()
    runner.bootstrap_runtime()
    result = runner.run_autopilot()
    assert_expected(expected, result)
