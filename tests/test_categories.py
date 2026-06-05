"""Offline tests for the bundled category catalog and name/id resolution."""
import pytest

from kleinanzeigen_api import KleinanzeigenAPI, Category, all_categories, find_categories
from kleinanzeigen_api.categories import flatten_api_categories, get_category, resolve_category


def test_catalog_loads():
    cats = all_categories()
    assert len(cats) >= 150
    assert all(isinstance(c, Category) for c in cats)
    # Wohnung mieten is the canonical rental category
    wm = get_category(203)
    assert wm is not None and wm.real_estate is True and "miet" in wm.name.lower()


def test_find_categories_ranks_exact_first():
    hits = find_categories("Autos")
    assert hits and hits[0].name == "Autos" and hits[0].id == "216"


def test_find_categories_substring_and_empty():
    assert any("Fahrr" in c.name for c in find_categories("Fahrr"))
    assert find_categories("") == []
    assert find_categories("definitely-not-a-category") == []


def test_resolve_category_passthrough_and_none():
    assert resolve_category(None) is None
    assert resolve_category("") is None
    assert resolve_category(217) == "217"
    assert resolve_category("203") == "203"


def test_resolve_category_by_exact_name():
    assert resolve_category("Autos") == "216"
    assert resolve_category("autos") == "216"  # case-insensitive
    # names documented in the README must resolve
    assert resolve_category("Mietwohnungen") == "203"
    assert resolve_category("Notebooks") == "278"
    assert resolve_category("Fahrräder & Zubehör") == "217"


def test_resolve_category_unknown_raises_with_hint():
    with pytest.raises(ValueError) as e:
        resolve_category("zzzz-nope")
    assert "find_categories" in str(e.value)


def test_search_rejects_both_category_and_id(monkeypatch):
    api = KleinanzeigenAPI()
    monkeypatch.setattr(api, "search_page", lambda **kw: (0, []))
    with pytest.raises(ValueError):
        api.search(category="Autos", category_id=216)


def test_search_resolves_category_name(monkeypatch):
    captured = {}
    api = KleinanzeigenAPI()

    def fake(**kw):
        captured.update(kw)
        return (0, [])

    monkeypatch.setattr(api, "search_page", fake)
    api.search(location=None, category="Autos")
    assert captured["category_id"] == "216"


def test_flatten_matches_bundled_schema():
    # synthetic mini-payload shaped like /api/categories.json
    payload = {
        "{http://www.ebayclassifiedsgroup.com/schema/category/v1}categories": {
            "value": {"category": [{
                "id-name": {"value": "Alle Kategorien"},
                "localized-name": {"value": "Alle Kategorien"},
                "category": [{
                    "id-name": {"value": "Immobilien"},
                    "localized-name": {"value": "Immobilien"},
                    "category": [{
                        "id-name": {"value": "Wohnung_Mieten"},
                        "localized-name": {"value": "Mietwohnungen"},
                        "id": "203", "category": [],
                    }],
                }],
            }]},
        }
    }
    flat = flatten_api_categories(payload)
    assert flat == [{
        "id": "203", "name": "Mietwohnungen",
        "path": "Immobilien > Mietwohnungen", "real_estate": True,
    }]
