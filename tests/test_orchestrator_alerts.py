import copy

from app.orchestrator import StrategyOrchestrator


class _PlanProvider:
    def __init__(self, initial_plan: dict) -> None:
        self.plan = initial_plan

    def __call__(self) -> dict:
        return copy.deepcopy(self.plan)


class _FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def alert_ops(
        self,
        *,
        text: str,
        kind: str = "ops_alert",
        extra: dict[str, object] | None = None,
    ) -> None:
        self.calls.append({"text": text, "kind": kind, "extra": extra or {}})


def test_orchestrator_emits_alerts_once(monkeypatch) -> None:
    orchestrator = StrategyOrchestrator(strategies={"cross_exchange_arb": 0, "delta_maker": 5})

    plan_payload = {
        "ts": "2024-01-02T03:04:05+00:00",
        "risk_gates": {
            "risk_caps_ok": False,
            "reason_if_blocked": "hold_active",
            "hold_active": True,
            "safe_mode": False,
            "autopilot_enabled": False,
        },
        "strategies": [
            {
                "name": "cross_exchange_arb",
                "decision": "skip",
                "reason": "hold_active",
                "last_result": "ok",
            },
            {
                "name": "delta_maker",
                "decision": "cooldown",
                "reason": "recent_error",
                "last_result": "fail",
            },
        ],
    }

    provider = _PlanProvider(plan_payload)
    monkeypatch.setattr(orchestrator, "compute_next_plan", provider)

    fake_notifier = _FakeNotifier()

    orchestrator.emit_alerts_if_needed(fake_notifier)

    assert [call["text"] for call in fake_notifier.calls] == [
        "[orchestrator] strategy=cross_exchange_arb decision=skip reason=hold_active autopilot=OFF",
        "[orchestrator] strategy=delta_maker decision=cooldown reason=recent_error autopilot=OFF",
    ]
    assert fake_notifier.calls[0]["kind"] == "orchestrator_alert"
    assert fake_notifier.calls[1]["extra"]["last_result"] == "fail"

    orchestrator.emit_alerts_if_needed(fake_notifier)
    assert len(fake_notifier.calls) == 2

    provider.plan = {
        "ts": "2024-01-02T03:04:10+00:00",
        "risk_gates": {
            "risk_caps_ok": False,
            "reason_if_blocked": "risk_limit",
            "hold_active": False,
            "safe_mode": False,
            "autopilot_enabled": True,
        },
        "strategies": [
            {
                "name": "cross_exchange_arb",
                "decision": "skip",
                "reason": "risk_limit",
                "last_result": "ok",
            },
            {
                "name": "delta_maker",
                "decision": "cooldown",
                "reason": "recent_error",
                "last_result": "fail",
            },
        ],
    }

    orchestrator.emit_alerts_if_needed(fake_notifier)
    assert len(fake_notifier.calls) == 4
    assert fake_notifier.calls[-2]["text"].endswith("autopilot=ON")

