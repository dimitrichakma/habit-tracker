"""What one sample is, and the sampler interface. No macOS imports here."""

from dataclasses import dataclass
from typing import Protocol

from .. import config

ACCESSIBILITY_MESSAGE = (
    "Window titles are unavailable: Accessibility permission is not granted. "
    "Tracking continues with app names only. To fix it, open System Settings > "
    "Privacy & Security > Accessibility and enable the app you run this from "
    "(e.g. Terminal), then restart the tracker."
)


@dataclass(frozen=True)
class Sample:
    ts: int  # UTC unix epoch, whole seconds
    app: str
    window_title: str | None  # None = could not be read
    idle_seconds: float
    issue: str | None = None  # runtime-only note (e.g. missing permission); never stored


class Sampler(Protocol):
    def sample(self) -> Sample: ...


def is_idle(sample: Sample, threshold: float = config.IDLE_THRESHOLD_SECONDS) -> bool:
    return sample.idle_seconds >= threshold
