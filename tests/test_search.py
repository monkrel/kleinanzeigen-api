"""Offline tests for how search() combines pages: the exclude filter, the
all-category default, and the search_rentals shortcut. search_page is replaced
with a fake so no network request is made."""
from kleinanzeigen_api import KleinanzeigenAPI, Listing
from kleinanzeigen_api.client import _as_terms, _excluded


def mk(id, title, description="", rooms=None, size_m2=None):
    return Listing(id=id, title=title, description=description, price=None,
                   price_type="", url="", city="", zip_code="", latitude=None,
                   longitude=None, size_m2=size_m2, rooms=rooms, posted="",
                   poster_type="")


def test_as_terms_normalizes():
    assert _as_terms(None) == []
    assert _as_terms("") == []
    assert _as_terms("iPhone") == ["iphone"]
    assert _as_terms([" Defekt ", "", "BASTLER"]) == ["defekt", "bastler"]


def test_excluded_matches_title_or_description():
    l = mk("1", "iPhone 13", "voll funktionsfähig")
    assert _excluded(l, ["iphone"]) is True
    assert _excluded(l, ["defekt"]) is False
    assert _excluded(mk("2", "Pixel 7", "leider defekt"), ["defekt"]) is True
    assert _excluded(l, []) is False


def test_search_applies_exclude(monkeypatch):
    api = KleinanzeigenAPI()
    page = [
        mk("1", "iPhone 13", "neu"),
        mk("2", "Samsung Galaxy", "tausche gegen iPhone"),  # excluded via description
        mk("3", "Google Pixel 7", "top Zustand"),
    ]
    monkeypatch.setattr(api, "search_page", lambda **kw: (len(page), page))
    out = api.search(location=None, q="handy", exclude="iphone")
    assert [l.id for l in out] == ["3"]


def test_search_page_default_is_all_categories():
    # the low-level page fetch must not silently restrict to rentals
    import inspect
    sig = inspect.signature(KleinanzeigenAPI.search_page)
    assert sig.parameters["category_id"].default is None


def test_search_defaults_to_all_categories(monkeypatch):
    captured = {}
    api = KleinanzeigenAPI()

    def fake(**kw):
        captured.update(kw)
        return (0, [])

    monkeypatch.setattr(api, "search_page", fake)
    api.search(location=None)
    assert captured["category_id"] is None


def test_search_rentals_sets_category(monkeypatch):
    captured = {}
    api = KleinanzeigenAPI()

    def fake(**kw):
        captured.update(kw)
        return (0, [])

    monkeypatch.setattr(api, "search_page", fake)
    api.search_rentals(location="3331")  # a numeric id skips the network lookup
    assert captured["category_id"] == "203"  # normalized to a string id
    assert captured["location_id"] == "3331"


def test_search_unknown_location_raises_loudly(monkeypatch):
    import pytest
    api = KleinanzeigenAPI()
    monkeypatch.setattr(api, "best_location", lambda q: None)   # unresolvable name
    monkeypatch.setattr(api, "search_page", lambda **kw: (0, []))
    with pytest.raises(ValueError) as e:
        api.search("Definitiv-keine-Stadt")
    assert "location=None" in str(e.value)  # the message should mention location=None


def test_search_none_location_is_allowed(monkeypatch):
    # location=None must not raise; it means "search all of Germany"
    captured = {}
    api = KleinanzeigenAPI()
    monkeypatch.setattr(api, "search_page",
                        lambda **kw: captured.update(kw) or (0, []))
    api.search(location=None, q="anything")
    assert captured["location_id"] is None


def test_search_dedupes_across_pages(monkeypatch):
    api = KleinanzeigenAPI()
    dup = mk("9", "same ad")

    monkeypatch.setattr(api, "search_page", lambda **kw: (100, [dup]))
    out = api.search(location=None, pages=3)
    assert [l.id for l in out] == ["9"]  # seen-set prevents duplicates
