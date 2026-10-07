"""Rule lookup from categories.yaml. Pure: no I/O beyond reading the shipped file once."""

from functools import lru_cache
from importlib.resources import files

import yaml

CATEGORIES = ("Work", "Learning", "Communication", "Social", "Entertainment", "Other")

Rules = dict[str, dict[str, list[str]]]


@lru_cache(maxsize=1)
def load_rules() -> Rules:
    return yaml.safe_load(files("desktop_tracker").joinpath("categories.yaml").read_text())


def _keyword_hit(rules: Rules, text: str | None) -> str | None:
    if not text:
        return None
    text = text.lower()
    for category in CATEGORIES:  # fixed order breaks ties
        for keyword in rules.get(category, {}).get("keywords", []):
            if keyword.lower() in text:
                return category
    return None


def match(
    app: str, site: str | None, title: str | None, rules: Rules | None = None
) -> str | None:
    """Category for an activity, or None. Title keyword > site keyword > app name."""
    rules = rules if rules is not None else load_rules()
    hit = _keyword_hit(rules, title) or _keyword_hit(rules, site)
    if hit:
        return hit
    name = app.lower()
    for category in CATEGORIES:
        if any(name == a.lower() for a in rules.get(category, {}).get("apps", [])):
            return category
    return None
