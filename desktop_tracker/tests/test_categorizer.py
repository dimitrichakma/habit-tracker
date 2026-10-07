from desktop_tracker.categorize import CATEGORIES, categorize


def test_known_app_gets_its_category():
    assert categorize("Slack") == "Communication"


def test_title_keyword_is_used():
    assert categorize("Some Browser", "Python tutorial") == "Learning"


def test_site_is_used():
    assert categorize("Some Browser", None, site="www.youtube.com") == "Entertainment"


def test_unknown_app_becomes_other():
    assert categorize("Zzz Totally Unknown App") == "Other"
    assert categorize("Zzz Totally Unknown App", "untitled") == "Other"


def test_categories_are_the_fixed_list_in_order():
    assert CATEGORIES == (
        "Work", "Learning", "Communication", "Social", "Entertainment", "Other",
    )
