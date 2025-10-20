from __future__ import annotations
from fastapi import APIRouter
from prometheus_client import Histogram
from starlette.responses import PlainTextResponse
import random

from ..services.runtime import append_latency_sample

router = APIRouter()

LAT_HIST = Histogram("app_latency_ms", "Synthetic latency histogram", buckets=(5, 10, 25, 50, 100, 200, 400, 800, 1600))


@router.get("/latency")
def latency_dump() -> PlainTextResponse:
    # pump some synthetic samples (paper mode)
    for _ in range(5):
        value = random.uniform(5, 120)
        LAT_HIST.observe(value)
        append_latency_sample(value)
    return PlainTextResponse("ok")
