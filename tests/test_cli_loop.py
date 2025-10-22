from app import ledger
from app.cli import main as cli_main
from app.services.runtime import get_loop_state, get_state, reset_for_tests


def test_cli_loop_runs_single_cycle():
    reset_for_tests()
    ledger.reset()
    state = get_state()
    state.control.safe_mode = False
    state.control.dry_run = True
    exit_code = cli_main.main(
        [
            "loop",
            "--env",
            "paper",
            "--pair",
            "BTCUSDT",
            "--venues",
            "binance-um",
            "okx-perp",
            "--cycles",
            "1",
            "--notional",
            "25",
        ]
    )
    assert exit_code == 0
    loop_state = get_loop_state()
    assert loop_state.cycles_completed == 1
    assert loop_state.last_plan is not None
    assert state.control.loop_pair == "BTCUSDT"
    assert state.control.loop_venues == ["binance-um", "okx-perp"]
