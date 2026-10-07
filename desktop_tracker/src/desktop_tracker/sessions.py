"""Group raw samples into sessions: the same app, in a row, while not idle.

Only gaps in sampling (missed ticks) are bridged. Switching to another app always
starts a new session, even for a few seconds. A session's `seconds` is the number of
samples in it times the sample interval, so time we never observed is never invented.
"""

from dataclasses import dataclass

from . import config
from .sampler.base import Sample, is_idle

MAX_GAP_SECONDS = 30  # a gap this long or longer between samples splits a session


@dataclass(frozen=True)
class Session:
    app: str
    start: int  # UTC epoch, first sample
    end: int  # UTC epoch, last sample + one interval
    seconds: int  # samples x interval


def build_sessions(
    samples: list[Sample],
    interval: int = config.SAMPLE_INTERVAL_SECONDS,
    max_gap: int = MAX_GAP_SECONDS,
) -> list[Session]:
    sessions: list[Session] = []
    app: str | None = None
    start = last = count = 0

    def close() -> None:
        if app is not None:
            sessions.append(Session(app, start, last + interval, count * interval))

    for sample in samples:
        if is_idle(sample):
            close()
            app = None
            continue
        continues = app == sample.app and sample.ts - last < max_gap
        if continues:
            last = sample.ts
            count += 1
            continue
        close()
        app, start, last, count = sample.app, sample.ts, sample.ts, 1
    close()
    return sessions
