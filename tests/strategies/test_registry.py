from app.strategies.registry import (
    StrategyInfo,
    StrategyRegistry,
    get_strategy_registry,
    register_default_strategies,
)


def test_strategy_registry_register_and_get() -> None:
    reg = StrategyRegistry()
    info = StrategyInfo(
        id="xex_arb",
        name="Cross-exchange arbitrage",
        description="test",
        tags=["arb"],
    )
    reg.register(info)
    got = reg.get("xex_arb")
    assert got is not None
    assert got.id == "xex_arb"


def test_register_default_strategies_populates_global_registry() -> None:
    register_default_strategies()
    reg = get_strategy_registry()
    ids = {entry.id for entry in reg.all()}
    assert "xex_arb" in ids
