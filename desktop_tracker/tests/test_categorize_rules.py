from desktop_tracker.categorize.rules import load_rules, match

RULES = {
    "Work": {"apps": ["Editor"], "keywords": ["spreadsheet"]},
    "Learning": {"apps": [], "keywords": ["lesson"]},
    "Communication": {"apps": [], "keywords": []},
    "Social": {"apps": [], "keywords": ["chatter"]},
    "Entertainment": {"apps": ["Player"], "keywords": ["videos"]},
    "Other": {"apps": [], "keywords": []},
}


def test_app_match_is_case_insensitive_and_exact():
    assert match("editor", None, None, rules=RULES) == "Work"
    assert match("EDITOR", None, None, rules=RULES) == "Work"
    assert match("Editor Pro", None, None, rules=RULES) is None


def test_keyword_in_title_matches_case_insensitively():
    assert match("Browser", None, "My LESSON notes", rules=RULES) == "Learning"


def test_keyword_in_site_matches():
    assert match("Browser", "www.chatter.example", None, rules=RULES) == "Social"


def test_no_match_returns_none():
    assert match("Nothing", "nowhere.example", "blank", rules=RULES) is None
    assert match("Nothing", None, None, rules=RULES) is None


def test_title_keyword_beats_site_keyword():
    assert match("Browser", "chatter.example", "lesson 1", rules=RULES) == "Learning"


def test_site_keyword_beats_app_name():
    assert match("Editor", "chatter.example", None, rules=RULES) == "Social"


def test_title_keyword_beats_app_name():
    assert match("Player", None, "lesson 1", rules=RULES) == "Learning"


def test_same_level_tie_goes_to_fixed_category_order():
    # Work comes before Learning in the fixed order.
    assert match("Browser", None, "spreadsheet lesson", rules=RULES) == "Work"


def test_load_rules_reads_the_shipped_file():
    rules = load_rules()
    assert "Work" in rules and "apps" in rules["Work"]


# --- the shipped categories.yaml ---


def test_youtube_tutorial_tab_is_learning():
    assert match("Google Chrome", "www.youtube.com", "Python tutorial for beginners") == "Learning"


def test_youtube_tab_with_no_match_is_entertainment():
    assert match("Google Chrome", "www.youtube.com", "Funny cats compilation") == "Entertainment"


def test_youtube_with_no_title_is_entertainment():
    assert match("Google Chrome", "www.youtube.com", None) == "Entertainment"


def test_shipped_app_names_resolve():
    assert match("Xcode", None, None) == "Work"
    assert match("Slack", None, None) == "Communication"
