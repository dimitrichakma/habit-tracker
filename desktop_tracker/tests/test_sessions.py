from desktop_tracker.sampler.base import Sample
from desktop_tracker.sessions import Session, build_sessions

STEP = 5  # SAMPLE_INTERVAL_SECONDS


def s(ts, app="Notes", idle=0.0, title=None) -> Sample:
    return Sample(ts=ts, app=app, window_title=title, idle_seconds=idle)


def test_no_samples_no_sessions():
    assert build_sessions([]) == []


def test_consecutive_same_app_is_one_session():
    sessions = build_sessions([s(100), s(105), s(110)])
    assert len(sessions) == 1
    only = sessions[0]
    assert isinstance(only, Session)
    assert only.app == "Notes"
    assert only.start == 100
    assert only.end == 115  # last sample + one interval
    assert only.seconds == 15


def test_single_sample_counts_one_interval():
    [only] = build_sessions([s(100)])
    assert (only.start, only.end, only.seconds) == (100, 105, STEP)


def test_app_switch_starts_a_new_session():
    sessions = build_sessions([s(100, "Notes"), s(105, "Notes"), s(110, "Slack")])
    assert [(x.app, x.start, x.seconds) for x in sessions] == [
        ("Notes", 100, 10),
        ("Slack", 110, 5),
    ]


def test_switching_away_and_back_is_not_bridged():
    # A, B, A: a detour to another app always splits. Only sampling gaps bridge.
    sessions = build_sessions([s(100, "A"), s(105, "B"), s(110, "A")])
    assert [x.app for x in sessions] == ["A", "B", "A"]


def test_sampling_gap_under_30s_is_bridged():
    # 25s with no samples (a missed tick or two) between the same app: still one session.
    sessions = build_sessions([s(100), s(125)])
    assert len(sessions) == 1
    assert sessions[0].start == 100
    assert sessions[0].end == 130


def test_sampling_gap_of_exactly_30s_splits():
    assert len(build_sessions([s(100), s(130)])) == 2


def test_sampling_gap_over_30s_splits():
    assert len(build_sessions([s(100), s(200)])) == 2


def test_bridged_gap_time_is_not_counted():
    # Seconds are samples x interval; the 20s we never observed is not invented.
    [only] = build_sessions([s(100), s(120)])
    assert only.seconds == 2 * STEP


def test_idle_samples_are_excluded():
    sessions = build_sessions([s(100, idle=500.0), s(105, idle=500.0)])
    assert sessions == []


def test_idle_in_the_middle_splits_the_session():
    sessions = build_sessions([s(100), s(105, idle=500.0), s(110)])
    assert [(x.start, x.seconds) for x in sessions] == [(100, 5), (110, 5)]


def test_just_under_idle_threshold_still_counts_as_active():
    [only] = build_sessions([s(100, idle=119.9)])
    assert only.seconds == STEP


def test_total_session_time_equals_active_sample_count_times_interval():
    samples = [
        s(100, "A"), s(105, "A"), s(110, "B"), s(115, "B", idle=300.0),
        s(120, "B"), s(200, "A"), s(205, "A"),
    ]
    active = [x for x in samples if x.idle_seconds < 120]
    total = sum(x.seconds for x in build_sessions(samples))
    assert total == len(active) * STEP
