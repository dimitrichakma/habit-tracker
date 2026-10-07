"""Today's time per category: the sum of non-idle samples, each worth one interval."""

from datetime import date, datetime, time, timedelta

from . import config
from .categorize import CATEGORIES
from .categorize.resolver import CategoryResolver
from .sampler.base import is_idle
from .store import ActivityStore


def today() -> date:
    return date.today()


def _day_bounds(day: date) -> tuple[int, int]:
    """Local midnight to local midnight, as UTC epoch seconds (the store's format)."""
    start = datetime.combine(day, time.min).astimezone()
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone()
    return int(start.timestamp()), int(end.timestamp())


def time_per_category(
    store: ActivityStore,
    day: date,
    interval: int = config.SAMPLE_INTERVAL_SECONDS,
    classifier=None,
    out=print,
) -> dict[str, int]:
    """Unknown apps/sites are resolved here (cache, then `classifier`), never while tracking."""
    totals = dict.fromkeys(CATEGORIES, 0)
    resolver = CategoryResolver(store, classifier, out=out)
    for sample in store.between(*_day_bounds(day)):
        if not is_idle(sample):
            totals[resolver.resolve(sample.app, sample.site, sample.window_title)] += interval
    return totals


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def format_report(totals: dict[str, int]) -> str:
    lines = [f"{c:<15}{format_duration(totals[c])}" for c in CATEGORIES if totals[c]]
    if not lines:
        return "No activity recorded today."
    lines.append(f"{'Total':<15}{format_duration(sum(totals.values()))}")
    return "\n".join(lines)


def run_today(store: ActivityStore, out=print, classifier=None) -> None:
    out(format_report(time_per_category(store, today(), classifier=classifier, out=out)))
