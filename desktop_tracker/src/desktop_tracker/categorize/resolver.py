"""Report-time category resolution: rules, then the cache, then local Ollama.

Never used inside the 5-second tracking loop. One resolver per report: if Ollama fails
once it is skipped for the rest of that report, and a failure or an invalid answer is
never cached (so the next report asks again).
"""

from .rules import match


class CategoryResolver:
    def __init__(self, store, classifier=None, out=print):
        self._store = store
        self._classifier = classifier
        self._out = out
        self._memo: dict[tuple[str, str], str] = {}
        self._ollama_failed = False

    def resolve(self, app: str, site: str | None, title: str | None) -> str:
        # A tab's host is the better signal than the browser's own app rule (Other), so
        # with a site the app name is not consulted: rules on title/site, then the site.
        hit = match("" if site else app, site, title)
        if hit:
            return hit
        kind, name = ("site", site) if site else ("app", app)
        return self._unknown(kind, name.lower(), name)

    def _unknown(self, kind: str, key: str, name: str) -> str:
        if (kind, key) in self._memo:
            return self._memo[(kind, key)]
        cached = self._store.get_category(kind, key)
        if cached:
            return cached
        if self._classifier is None or self._ollama_failed:
            return "Other"
        try:
            answer = self._classifier.classify(name, kind)
        except Exception as exc:
            self._ollama_failed = True
            self._out(
                f"[categories] Ollama unavailable ({type(exc).__name__}); "
                "unknown apps and sites count as Other in this report."
            )
            return "Other"
        if answer is None:  # invalid answer: Other, not cached
            self._memo[(kind, key)] = "Other"
            return "Other"
        self._store.set_category(kind, key, answer)
        self._memo[(kind, key)] = answer
        return answer
