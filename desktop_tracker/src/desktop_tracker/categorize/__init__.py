from .rules import CATEGORIES, match


def categorize(app: str, title: str | None = None, site: str | None = None) -> str:
    """One of CATEGORIES. Anything the rules don't know is Other."""
    return match(app, site, title) or "Other"


__all__ = ["CATEGORIES", "categorize"]
