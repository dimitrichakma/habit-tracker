"""Local Ollama classifier for apps/sites the rules don't know.

Sends ONLY an app name or a site host, never a window title. Localhost only. The model
must pick one of the six categories; anything else is "no answer" (None). Errors
propagate so the caller can skip Ollama and avoid caching a guess.
"""

import json
from urllib.parse import urlparse

import ollama

from .. import config
from .rules import CATEGORIES

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}},
    "required": ["category"],
}
_SYSTEM = (
    "You classify a Mac application or a website into exactly one category: "
    + ", ".join(CATEGORIES)
    + ". Reply with JSON like {\"category\": \"Work\"}. Use Other if unsure."
)


def assert_local_host(host: str) -> None:
    """Raise ValueError unless `host` points at this machine."""
    url = host if "://" in host else f"http://{host}"
    try:
        name = urlparse(url).hostname
    except ValueError:
        name = None
    if name not in _LOCAL_HOSTS:
        raise ValueError(f"Ollama must be on localhost, refusing {host!r}")


def _validate(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    wanted = raw.strip().casefold()
    return next((c for c in CATEGORIES if c.casefold() == wanted), None)


class OllamaClassifier:
    def __init__(self, model=None, host=None, timeout=None, client=None):
        self.host = host if host is not None else config.OLLAMA_HOST
        assert_local_host(self.host)
        self.model = model or config.OLLAMA_MODEL
        self.timeout = timeout if timeout is not None else config.OLLAMA_TIMEOUT_SECONDS
        self._client = client

    def _get_client(self):
        if self._client is None:
            self._client = ollama.Client(host=self.host, timeout=self.timeout)
        return self._client

    def classify(self, name: str, kind: str) -> str | None:
        """A valid category, or None if the model's answer wasn't one. Raises on failure."""
        label = "website" if kind == "site" else "application"
        reply = self._get_client().chat(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"{label}: {name}"},
            ],
            think=False,
            format=_SCHEMA,
            options={"temperature": 0},
        )
        content = reply["message"]["content"]
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return _validate(content)
        return _validate(parsed.get("category")) if isinstance(parsed, dict) else None
