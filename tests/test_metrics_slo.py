import time

from app.metrics import slo


def test_inc_skipped_increments_counter():
    slo.reset_for_tests()
    before = slo.SKIPPED_COUNTER.labels(reason="risk_gate")._value.get()
    slo.inc_skipped("risk_gate")
    after = slo.SKIPPED_COUNTER.labels(reason="risk_gate")._value.get()
    assert after == before + 1


def test_order_cycle_timer_records_latency_sample():
    slo.reset_for_tests()
    with slo.order_cycle_timer():
        time.sleep(0.001)
    metrics = {
        sample.name: sample.value
        for metric in slo.ORDER_CYCLE_HISTOGRAM.collect()
        for sample in metric.samples
    }
    assert metrics.get("propbot_order_cycle_ms_count", 0) >= 1
    assert metrics.get("propbot_order_cycle_ms_sum", 0.0) >= 0.0
