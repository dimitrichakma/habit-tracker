"""Rule lookup from categories.yaml. Pure: no I/O beyond reading the shipped file once."""

from functools import lru_cache
from importlib.resources import files

import yaml

CATEGORIES = ("Work", "Learning", "Communication", "Social", "Entertainment", "Other")

Rules = dict[str, dict[str, list[str]]]


@lru_cache(maxsize=1)
def load_rules() -> Rules:
    return yaml.safe_load(files("desktop_tracker").joinpath("categories.yaml").read_text())


def _keyword_hit(
    rules: Rules, text: str | None, ignore_in: str | None = None
) -> str | None:
    """First category (fixed order) with a keyword in `text`. Keywords that also appear
    in `ignore_in` are skipped: they add nothing the other signal doesn't already say."""
    if not text:
        return None
    text = text.lower()
    ignore_in = (ignore_in or "").lower()
    for category in CATEGORIES:  # fixed order breaks ties
        for keyword in rules.get(category, {}).get("keywords", []):
            keyword = keyword.lower()
            if keyword in text and not (ignore_in and keyword in ignore_in):
                return category
    return None


def match(
    app: str, site: str | None, title: str | None, rules: Rules | None = None
) -> str | None:
    """Category for an activity, or None. Title keyword > site keyword > app name.

    A title keyword that is just the site's own name ("YouTube" in "... - YouTube" on
    youtube.com) is ignored at the title level, so a content word like "tutorial" beats
    the site keyword whatever the category order."""
    rules = rules if rules is not None else load_rules()
    hit = _keyword_hit(rules, title, ignore_in=site) or _keyword_hit(rules, site)
    if hit:
        return hit
    name = app.lower()
    for category in CATEGORIES:
        if any(name == a.lower() for a in rules.get(category, {}).get("apps", [])):
            return category
    return None
