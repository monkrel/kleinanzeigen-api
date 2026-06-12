"""Offline tests for API-backed location resolution and search metadata.

All network access is monkeypatched; these exercise the response parsing and
the website fallback path only.
"""
import pytest

from kleinanzeigen_api import KleinanzeigenAPI
from kleinanzeigen_api.client import LOCATIONS_NS, SEARCH_META_NS


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# --- /api/locations.json shape (trimmed to the fields we parse) ------------- #
def _locations_payload():
    return {
        LOCATIONS_NS: {"value": {"location": [{
            "id": "3331",
            "id-name": {"value": "Berlin"},
            "localized-name": {"value": "Berlin"},
            "location": [{
                "id": "3360",
                "localized-name": {"value": "Charlottenburg (Berlin)"},
                "location": [{
                    "id": "107405",
                    "localized-name": {"value": "13627 Charlottenburg"},
                    "location": [],
                }],
            }],
        }]}},
    }


def test_resolve_location_api_flattens_tree(monkeypatch):
    api = KleinanzeigenAPI()
    monkeypatch.setattr(api, "_get", lambda url, params=None: _Resp(_locations_payload()))
    cands = api.resolve_location("Berlin")
    # parent before children, ids stringified
    assert cands[0] == ("3331", "Berlin")
    ids = [c[0] for c in cands]
    assert ids == ["3331", "3360", "107405"]


def test_resolve_location_single_node_not_list(monkeypatch):
    api = KleinanzeigenAPI()
    payload = {LOCATIONS_NS: {"value": {"location": {
        "id": "3331", "localized-name": {"value": "Berlin"}, "location": [],
    }}}}
    monkeypatch.setattr(api, "_get", lambda url, params=None: _Resp(payload))
    assert api.resolve_location("Berlin") == [("3331", "Berlin")]


def test_resolve_location_falls_back_to_website(monkeypatch):
    api = KleinanzeigenAPI()

    def boom(url, params=None):
        raise RuntimeError("403 from API")

    monkeypatch.setattr(api, "_get", boom)
    monkeypatch.setattr(api, "_resolve_location_web",
                        lambda q: [("999", "Fallbackcity - State")])
    assert api.resolve_location("Fallbackcity") == [("999", "Fallbackcity - State")]


def test_resolve_location_empty_api_uses_website(monkeypatch):
    api = KleinanzeigenAPI()
    monkeypatch.setattr(api, "_get",
                        lambda url, params=None: _Resp({LOCATIONS_NS: {"value": {"location": []}}}))
    monkeypatch.setattr(api, "_resolve_location_web", lambda q: [("1", "from-web")])
    assert api.resolve_location("x") == [("1", "from-web")]


def test_best_location_matches_api_label_style(monkeypatch):
    api = KleinanzeigenAPI()
    # API label "Charlottenburg (Berlin)" should match query "charlottenburg"
    monkeypatch.setattr(api, "resolve_location",
                        lambda q: [("3331", "Berlin"), ("3360", "Charlottenburg (Berlin)")])
    assert api.best_location("charlottenburg") == ("3360", "Charlottenburg (Berlin)")


# --- /api/ads/search-metadata/{cat}.json ------------------------------------ #
def _metadata_payload():
    return {SEARCH_META_NS: {"value": {
        "q": {"type": "STRING", "localized-label": "Titel", "search-param": "optional"},
        "minPrice": {"type": "DECIMAL", "localized-label": "Preis von",
                     "search-param": "optional"},
        "priceType": {"type": "ENUM", "localized-label": "Preis",
                      "search-param": "unsupported",
                      "supported-value": [
                          {"value": "FREE", "localized-label": "Zu verschenken"},
                          {"value": "SPECIFIED_AMOUNT", "localized-label": "Festpreis"},
                      ]},
    }}}


def test_search_metadata_parses_params_and_enums(monkeypatch):
    api = KleinanzeigenAPI()
    monkeypatch.setattr(api, "_get", lambda url, params=None: _Resp(_metadata_payload()))
    meta = api.search_metadata(category_id=203)
    assert meta["minPrice"] == {"label": "Preis von", "type": "DECIMAL",
                                "search_param": "optional"}
    assert meta["priceType"]["values"] == [("FREE", "Zu verschenken"),
                                           ("SPECIFIED_AMOUNT", "Festpreis")]
    assert "values" not in meta["q"]  # non-enum params carry no values list


def test_search_metadata_resolves_category_name(monkeypatch):
    api = KleinanzeigenAPI()
    seen = {}

    def fake_get(url, params=None):
        seen["url"] = url
        return _Resp(_metadata_payload())

    monkeypatch.setattr(api, "_get", fake_get)
    api.search_metadata("Mietwohnungen")
    assert seen["url"].endswith("/api/ads/search-metadata/203.json")


def test_search_metadata_requires_category():
    api = KleinanzeigenAPI()
    with pytest.raises(ValueError):
        api.search_metadata()
    with pytest.raises(ValueError):
        api.search_metadata(category="Autos", category_id=216)
