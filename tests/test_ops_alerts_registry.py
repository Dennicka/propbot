from datetime import datetime, timedelta, timezone

import pytest

from app.alerts.registry import OpsAlert, OpsAlertsRegistry


def _make_alert(
    *,
    index: int,
    event_type: str = "generic",
    severity: str = "info",
) -> OpsAlert:
    return OpsAlert(
        ts=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index),
        event_type=event_type,
        message=f"alert-{index}",
        severity=severity,
        source="tests",
        context={"idx": index},
    )


def test_registry_returns_latest_alerts_first() -> None:
    registry = OpsAlertsRegistry(maxlen=10)
    for idx in range(5):
        registry.add(_make_alert(index=idx))

    alerts = registry.list_recent(limit=5)

    assert [alert.message for alert in alerts] == [
        "alert-4",
        "alert-3",
        "alert-2",
        "alert-1",
        "alert-0",
    ]


def test_registry_filters_by_event_type_and_severity() -> None:
    registry = OpsAlertsRegistry(maxlen=10)
    registry.add(_make_alert(index=0, event_type="alpha", severity="info"))
    registry.add(_make_alert(index=1, event_type="alpha", severity="critical"))
    registry.add(_make_alert(index=2, event_type="beta", severity="warning"))

    alpha_alerts = registry.list_recent(event_type="alpha")
    assert [alert.event_type for alert in alpha_alerts] == ["alpha", "alpha"]

    critical_alerts = registry.list_recent(severity="critical")
    assert len(critical_alerts) == 1
    assert critical_alerts[0].severity == "critical"
    assert critical_alerts[0].message == "alert-1"


def test_registry_respects_limit_and_maxlen() -> None:
    registry = OpsAlertsRegistry(maxlen=3)
    for idx in range(6):
        registry.add(_make_alert(index=idx))

    alerts = registry.list_recent(limit=2)
    assert [alert.message for alert in alerts] == ["alert-5", "alert-4"]

    all_alerts = registry.list_recent(limit=10)
    assert [alert.message for alert in all_alerts] == [
        "alert-5",
        "alert-4",
        "alert-3",
    ]


def test_registry_rejects_invalid_severity() -> None:
    registry = OpsAlertsRegistry(maxlen=5)
    registry.add(_make_alert(index=0, severity="info"))

    with pytest.raises(ValueError):
        registry.add(
            OpsAlert(
                ts=datetime.now(timezone.utc),
                event_type="oops",
                message="bad",
                severity="unknown",
            )
        )

    assert registry.list_recent()  # registry still usable
