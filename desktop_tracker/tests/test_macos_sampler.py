import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


def test_real_sampler_returns_a_sample_and_never_raises():
    pytest.importorskip("Quartz")
    from desktop_tracker.sampler.base import Sample
    from desktop_tracker.sampler.macos import MacSampler

    sample = MacSampler().sample()

    assert isinstance(sample, Sample)
    assert isinstance(sample.app, str) and sample.app
    assert sample.window_title is None or isinstance(sample.window_title, str)
    assert isinstance(sample.idle_seconds, float) and sample.idle_seconds >= 0
    assert isinstance(sample.ts, int)
    # ts is UTC epoch seconds: within a few seconds of "now" whatever the local timezone.
    assert abs(sample.ts - time.time()) < 5
