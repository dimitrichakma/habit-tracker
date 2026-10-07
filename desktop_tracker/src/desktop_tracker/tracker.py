"""The sampling loop. Never raises on a bad tick; reports problems without spamming."""

import time

from . import config


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def run(
    sampler,
    store,
    interval=config.SAMPLE_INTERVAL_SECONDS,
    sleep=time.sleep,
    max_samples=None,
    out=print,
):
    announced_issues: set[str] = set()  # permission issues: once per run
    last_error = None  # errors: quiet while identical to the previous one

    def report(kind: str, exc: Exception) -> None:
        nonlocal last_error
        key = (type(exc), str(exc))
        if key != last_error:
            out(f"[tracker] {kind}: {_describe(exc)}")
        last_error = key

    ticks = 0
    while max_samples is None or ticks < max_samples:
        ticks += 1
        try:
            sample = sampler.sample()
        except Exception as exc:
            report("sampler error", exc)
            sleep(interval)
            continue

        if sample.issue and sample.issue not in announced_issues:
            announced_issues.add(sample.issue)
            out(f"[tracker] {sample.issue}")

        try:
            store.add(sample)
        except Exception as exc:
            report("could not save sample", exc)
        else:
            last_error = None  # recovered
        sleep(interval)
