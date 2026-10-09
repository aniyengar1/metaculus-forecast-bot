"""
Single calibration hook applied to the final aggregated binary probability
before submission. k=1.0 is identity (no-op); k>1 sharpens toward 0/1, k<1
shrinks toward 0.5. Tune k later against our own resolved questions -- start
conservative (k=1.0).
"""
from __future__ import annotations

import math


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def calibrate_binary_probability(
    p: float, k: float, clip_min: float, clip_max: float
) -> float:
    scaled = sigmoid(k * logit(p))
    return min(max(scaled, clip_min), clip_max)
