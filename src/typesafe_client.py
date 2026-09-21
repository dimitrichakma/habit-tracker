"""One shared TypeSafe (System One / Jev) client — the only place this app
talks to a non-Claude model for judgment. Used for a small number of bounded
calls where plain regex/substring code can't reliably infer what the user
meant (habit-name resolution, frequency-phrase parsing): see `tools.py`'s
`_find_habit` and `schedule_classifier.py`. Deliberately NOT used for
anything the provider-split rule reserves for Claude — generation,
safety classification, or judging the coach's own output (CLAUDE.md's
"every model that generates, judges, or classifies text is Claude" is about
those paths; this is a data-entry disambiguation aid, not agent text).

Reads TYPESAFE_API_KEY from the environment (typesafe-sdk's own default
behavior). Unset/invalid key -> every call raises, and every caller here
catches that and falls back to its pre-TypeSafe behavior — see the fail-safe
comments in tools.py and schedule_classifier.py.
"""

from functools import lru_cache

from typesafe_sdk import TypeSafeClient


@lru_cache(maxsize=1)
def get_typesafe_client() -> TypeSafeClient:
    return TypeSafeClient()
