from __future__ import annotations

from bot.autoscale import AutoScaleEngine


def test_autoscale_targets_and_intervals_match_thresholds() -> None:
    engine = AutoScaleEngine()

    assert engine.get_daily_trade_target(20) == 5
    assert engine.get_daily_trade_target(120) == 25
    assert engine.get_daily_trade_target(3200) == 350

    assert engine.get_scan_interval(20) == 90
    assert engine.get_scan_interval(120) == 90
    assert engine.get_scan_interval(3200) == 30


def test_autoscale_phase_and_position_size_progress() -> None:
    engine = AutoScaleEngine()

    assert engine.get_phase_info(20).phase == 0
    assert engine.get_phase_info(20).mode == "Live (nano)"
    assert engine.get_phase_info(120).phase == 1
    assert engine.get_phase_info(1600).phase == 4
    assert engine.get_max_position_size(20) == 5.0
    assert engine.get_max_position_size(120) == 3.0
    assert engine.get_max_position_size(3200) == 150.0
