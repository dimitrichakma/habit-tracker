from importlib.resources import files

import yaml

SIX = ["Work", "Learning", "Communication", "Social", "Entertainment", "Other"]


def load_raw() -> dict:
    return yaml.safe_load(files("desktop_tracker").joinpath("categories.yaml").read_text())


def test_keys_are_exactly_the_six_categories():
    assert sorted(load_raw()) == sorted(SIX)


def test_each_category_has_apps_and_keywords_lists():
    for name, body in load_raw().items():
        assert isinstance(body["apps"], list), name
        assert isinstance(body["keywords"], list), name


def test_entries_are_non_empty_strings():
    for name, body in load_raw().items():
        for entry in body["apps"] + body["keywords"]:
            assert isinstance(entry, str) and entry.strip(), name


def test_every_keyword_is_at_least_four_characters():
    for name, body in load_raw().items():
        for keyword in body["keywords"]:
            assert len(keyword) >= 4, f"{name}: {keyword!r}"


def test_no_app_or_keyword_appears_twice():
    apps, keywords = [], []
    for body in load_raw().values():
        apps += [a.lower() for a in body["apps"]]
        keywords += [k.lower() for k in body["keywords"]]
    assert len(apps) == len(set(apps))
    assert len(keywords) == len(set(keywords))
