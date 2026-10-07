import argparse

from . import config, report, tracker
from .categorize.ollama_classifier import OllamaClassifier
from .sampler.macos import MacSampler
from .store import ActivityStore


def _track() -> None:
    store = ActivityStore()
    print(f"Tracking every {config.SAMPLE_INTERVAL_SECONDS}s -> {config.DB_PATH} (Ctrl-C to stop)")
    try:
        tracker.run(MacSampler(), store)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        store.close()


def _today() -> None:
    store = ActivityStore()
    try:
        report.run_today(store, classifier=OllamaClassifier())
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="desktop_tracker")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("track", help="record activity (default)")
    sub.add_parser("today", help="print today's time per category")
    args = parser.parse_args(argv)
    _today() if args.command == "today" else _track()


if __name__ == "__main__":
    main()
