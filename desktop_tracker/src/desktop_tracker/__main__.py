from . import config, tracker
from .sampler.macos import MacSampler
from .store import ActivityStore


def main() -> None:
    store = ActivityStore()
    print(f"Tracking every {config.SAMPLE_INTERVAL_SECONDS}s -> {config.DB_PATH} (Ctrl-C to stop)")
    try:
        tracker.run(MacSampler(), store)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        store.close()


if __name__ == "__main__":
    main()
