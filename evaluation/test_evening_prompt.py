"""Offline tests for the evening unlogged-habits prompt, using a fake clock."""

from datetime import datetime

import pytest

from src import scheduler
from src.bot import TodayItem, TodayView
from src.scheduler import EveningPromptState, evening_prompt_config, run_evening_prompt


class FakeClock:
    def __init__(self, when: datetime) -> None:
        self.when = when

    def __call__(self) -> datetime:
        return self.when


def _view(*states: str) -> TodayView:
    return TodayView("Sat", [TodayItem(i, f"h{i}", s, None) for i, s in enumerate(states)])


class Harness:
    def __init__(self, view: TodayView) -> None:
        self.view = view
        self.sent: list[TodayView] = []
        self.fail = False

    async def build(self, day):
        return self.view

    async def send(self, view):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(view)


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 3, 21, 30))


async def _run(h, clock, state):
    return await run_evening_prompt(h.build, h.send, now=clock, state=state)


async def test_sends_only_pending_once_per_day(clock):
    h, state = Harness(_view("pending", "done", "earlier", "pending")), EveningPromptState()
    assert await _run(h, clock, state) is True
    assert [i.name for i in h.sent[0].items] == ["h0", "h3"]
    clock.when = datetime(2026, 10, 3, 21, 45)
    assert await _run(h, clock, state) is False
    assert len(h.sent) == 1


async def test_next_day_sends_again(clock):
    h, state = Harness(_view("pending")), EveningPromptState()
    await _run(h, clock, state)
    clock.when = datetime(2026, 10, 4, 21, 30)
    assert await _run(h, clock, state) is True
    assert len(h.sent) == 2


async def test_nothing_unlogged_sends_nothing_and_does_not_burn_the_day(clock):
    h, state = Harness(_view("done", "earlier")), EveningPromptState()
    assert await _run(h, clock, state) is False
    assert h.sent == []
    h.view = _view("pending")
    assert await _run(h, clock, state) is True


async def test_failed_send_can_retry(clock):
    h, state = Harness(_view("pending")), EveningPromptState()
    h.fail = True
    with pytest.raises(RuntimeError):
        await _run(h, clock, state)
    h.fail = False
    assert await _run(h, clock, state) is True


def test_config_defaults_and_overrides(monkeypatch):
    for k in ("EVENING_PROMPT_ENABLED", "EVENING_PROMPT_TIME"):
        monkeypatch.delenv(k, raising=False)
    assert evening_prompt_config() == (True, 21, 30)
    monkeypatch.setenv("EVENING_PROMPT_TIME", "22:05")
    assert evening_prompt_config() == (True, 22, 5)
    monkeypatch.setenv("EVENING_PROMPT_TIME", "garbage")
    assert evening_prompt_config() == (True, 21, 30)
    monkeypatch.setenv("EVENING_PROMPT_TIME", "25:00")
    assert evening_prompt_config() == (True, 21, 30)
    monkeypatch.setenv("EVENING_PROMPT_ENABLED", "false")
    assert evening_prompt_config()[0] is False


async def _job_ids(monkeypatch, enabled: str):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HABIT_TRACKER_USERNAME", "u")
    monkeypatch.setenv("EVENING_PROMPT_ENABLED", enabled)
    monkeypatch.delenv("EVENING_PROMPT_TIME", raising=False)
    monkeypatch.setattr(scheduler, "_resolve_user_id", lambda name: 1)
    sched = scheduler.start_reminder_scheduler(None, evening_view=Harness(_view()).build)
    try:
        return {j.id: j for j in sched.get_jobs()}
    finally:
        sched.shutdown(wait=False)


async def test_job_registered_when_enabled(monkeypatch):
    jobs = await _job_ids(monkeypatch, "true")
    fields = {f.name: str(f) for f in jobs[scheduler.EVENING_JOB_ID].trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("21", "30")


async def test_job_absent_when_disabled(monkeypatch):
    assert scheduler.EVENING_JOB_ID not in await _job_ids(monkeypatch, "false")
