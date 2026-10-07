from desktop_tracker import config
from desktop_tracker.sampler.base import Sample, is_idle


def make(idle: float) -> Sample:
    return Sample(ts=1_000, app="Xcode", window_title="main.swift", idle_seconds=idle)


def test_defaults_are_five_seconds_and_two_minutes():
    assert config.SAMPLE_INTERVAL_SECONDS == 5
    assert config.IDLE_THRESHOLD_SECONDS == 120


def test_not_idle_just_under_two_minutes():
    assert is_idle(make(119.9)) is False


def test_idle_at_exactly_two_minutes():
    assert is_idle(make(120.0)) is True


def test_idle_well_past_two_minutes():
    assert is_idle(make(3600)) is True


def test_not_idle_with_recent_input():
    assert is_idle(make(0.0)) is False


def test_custom_threshold_is_respected():
    assert is_idle(make(30), threshold=30) is True
    assert is_idle(make(29.9), threshold=30) is False
