"""Offline tests for parsing the capi JSON. No network needed."""
from kleinanzeigen_api import KleinanzeigenAPI, Listing
from kleinanzeigen_api.client import _num, _val

SAMPLE_AD = {
    "id": 123456,
    "title": {"value": "Schöne Wohnung &amp; Balkon"},
    "description": {"value": "Hell &amp; ruhig"},
    "price": {"amount": {"value": 750}, "price-type": {"value": "FIXED"}},
    "ad-address": {
        "state": {"value": "Berlin"},
        "zip-code": {"value": "10115"},
        "latitude": {"value": 52.53},
        "longitude": {"value": 13.38},
    },
    "attributes": {"attribute": [
        {"name": "wohnung_mieten.qm", "localized-label": "Wohnfläche",
         "value": [{"value": "34"}]},
        {"name": "wohnung_mieten.zimmer", "localized-label": "Zimmer",
         "value": [{"value": "2"}]},
        {"name": "wohnung_mieten.warmmiete", "localized-label": "Warmmiete",
         "value": [{"value": "920"}]},
    ]},
    "link": [
        {"rel": "self", "href": "https://api.kleinanzeigen.de/api/ads/123456.json"},
        {"rel": "self-public-website", "href": "https://www.kleinanzeigen.de/s-anzeige/x/123456"},
    ],
    "pictures": {"picture": [
        {"link": [
            {"rel": "teaser", "href": "https://img/teaser.jpg"},
            {"rel": "XXL", "href": "https://img/xxl.jpg"},
        ]},
    ]},
    "start-date-time": {"value": "2026-06-01T10:00:00.000Z"},
    "poster-type": {"value": "PRIVATE"},
}


def test_val_unwraps_nested():
    assert _val({"value": {"value": 5}}) == 5
    assert _val({"value": "x"}) == "x"
    assert _val("plain") == "plain"
    assert _val(None) is None


def test_num_coerces():
    assert _num("34") == 34.0
    assert _num(None) is None
    assert _num("not-a-number") is None


def test_parse_ad_core_fields():
    l = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    assert isinstance(l, Listing)
    assert l.id == "123456"
    assert l.price == 750.0
    assert l.price_type == "FIXED"
    assert l.city == "Berlin"
    assert l.zip_code == "10115"
    assert l.latitude == 52.53
    assert l.longitude == 13.38
    assert l.size_m2 == 34.0
    assert l.rooms == 2.0
    assert l.poster_type == "PRIVATE"


def test_parse_ad_html_unescaped():
    l = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    assert l.title == "Schöne Wohnung & Balkon"
    assert l.description == "Hell & ruhig"


def test_parse_ad_picks_public_url():
    l = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    assert l.url == "https://www.kleinanzeigen.de/s-anzeige/x/123456"


def test_parse_ad_collects_attributes_and_images():
    l = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    assert l.attributes["Warmmiete"] == "920"
    assert l.attributes["Wohnfläche"] == "34"
    assert l.images == ["https://img/xxl.jpg"]


def test_credentials_override_changes_auth_header():
    a = KleinanzeigenAPI(basic_user="u", basic_pw="p")
    b = KleinanzeigenAPI()
    assert a._auth != b._auth
    assert a._auth.startswith("Basic ")


def test_to_dict_roundtrip():
    l = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    d = l.to_dict()
    assert d["id"] == "123456"
    assert d["attributes"]["Zimmer"] == "2"
