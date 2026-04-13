from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PhaseInfo:
    phase: int
    label: str
    mode: str


class AutoScaleEngine:
    def get_daily_trade_target(self, bankroll: float) -> int:
        thresholds = [
            (50, 5),
            (150, 25),
            (400, 60),
            (800, 120),
            (1500, 200),
            (3000, 280),
            (float("inf"), 350),
        ]
        for limit, target in thresholds:
            if bankroll < limit:
                return target
        return 350

    def get_scan_interval(self, bankroll: float) -> int:
        target = self.get_daily_trade_target(bankroll)
        if target == 0:
            return 120
        if target <= 25:
            return 90
        if target <= 60:
            return 75
        if target <= 120:
            return 60
        if target <= 200:
            return 45
        if target <= 280:
            return 40
        return 30

    def get_max_position_size(self, bankroll: float) -> float:
        thresholds = [
            (50, 5.0),
            (150, 3.0),
            (400, 8.0),
            (800, 20.0),
            (1500, 40.0),
            (3000, 75.0),
            (10000, 150.0),
            (float("inf"), 500.0),
        ]
        for limit, position in thresholds:
            if bankroll < limit:
                return position
        return 500.0

    def get_phase_info(self, bankroll: float) -> PhaseInfo:
        phases = [
            (50, PhaseInfo(0, "Calibrate", "Live (nano)")),
            (150, PhaseInfo(1, "Validate", "Live (micro)")),
            (500, PhaseInfo(2, "Build", "Live (small)")),
            (1500, PhaseInfo(3, "Scale", "Live (medium)")),
            (5000, PhaseInfo(4, "Accelerate", "Live (full)")),
            (float("inf"), PhaseInfo(5, "Sustain", "Live (optimized)")),
        ]
        for limit, phase in phases:
            if bankroll < limit:
                return phase
        return PhaseInfo(5, "Sustain", "Live (optimized)")
