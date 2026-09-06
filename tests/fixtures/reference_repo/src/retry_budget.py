"""Small reference fixture outside the legacy capability list."""

import random


def decorrelated_jitter_delay(previous_delay: float, *, base: float, cap: float) -> float:
    """Bound retries while avoiding synchronized exponential backoff."""
    return min(cap, random.uniform(base, max(base, previous_delay * 3)))


def retry_schedule(*, attempts: int, base: float, cap: float) -> list[float]:
    delays: list[float] = []
    previous = base
    for _attempt in range(attempts):
        previous = decorrelated_jitter_delay(previous, base=base, cap=cap)
        delays.append(previous)
    return delays
