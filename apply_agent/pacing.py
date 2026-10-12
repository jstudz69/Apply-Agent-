"""Human-pace timing. Realistic delays matter more than any stealth trick."""

from __future__ import annotations

import random
import time


class Pacer:
    def __init__(self, speed: float = 1.0, seed: int | None = None):
        """speed > 1 is faster (tests use a large value); 1.0 is human pace."""
        self.speed = speed
        self.rng = random.Random(seed)

    def pause(self, low: float = 0.4, high: float = 1.2) -> None:
        time.sleep(self.rng.uniform(low, high) / self.speed)

    def think(self) -> None:
        """Longer pause, e.g. between pages or before reading a new question."""
        self.pause(1.5, 4.0)

    def key_delay_ms(self) -> float:
        """Per-keystroke delay for locator.press_sequentially."""
        return self.rng.uniform(45, 140) / self.speed
