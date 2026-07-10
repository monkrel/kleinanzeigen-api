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
    "attributes": {
        "attribute": [
            {
                "name": "wohnung_mieten.qm",
                "localized-label": "Wohnfläche",
                "value": [{"value": "34"}],
            },
            {
                "name": "wohnung_mieten.zimmer",
                "localized-label": "Zimmer",
                "value": [{"value": "2"}],
            },
            {
                "name": "wohnung_mieten.warmmiete",
                "localized-label": "Warmmiete",
                "value": [{"value": "920"}],
            },
        ]
    },
    "link": [
        {"rel": "self", "href": "https://api.kleinanzeigen.de/api/ads/123456.json"},
        {
            "rel": "self-public-website",
            "href": "https://www.kleinanzeigen.de/s-anzeige/x/123456",
        },
    ],
    "pictures": {
        "picture": [
            {
                "link": [
                    {"rel": "teaser", "href": "https://img/teaser.jpg"},
                    {"rel": "XXL", "href": "https://img/xxl.jpg"},
                ]
            },
        ]
    },
    "start-date-time": {"value": "2026-06-01T10:00:00.000Z"},
    "poster-type": {"value": "PRIVATE"},
    "category": {"id": "203", "localized-name": {"value": "Mietwohnungen"}},
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
    listing = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    assert isinstance(listing, Listing)
    assert listing.id == "123456"
    assert listing.price == 750.0
    assert listing.price_type == "FIXED"
    assert listing.city == "Berlin"
    assert listing.zip_code == "10115"
    assert listing.latitude == 52.53
    assert listing.longitude == 13.38
    assert listing.size_m2 == 34.0
    assert listing.rooms == 2.0
    assert listing.poster_type == "PRIVATE"
    assert listing.category_id == "203"


def test_parse_ad_html_unescaped():
    listing = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    assert listing.title == "Schöne Wohnung & Balkon"
    assert listing.description == "Hell & ruhig"


def test_parse_ad_picks_public_url():
    listing = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    assert listing.url == "https://www.kleinanzeigen.de/s-anzeige/x/123456"


def test_parse_ad_collects_attributes_and_images():
    listing = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    assert listing.attributes["Warmmiete"] == "920"
    assert listing.attributes["Wohnfläche"] == "34"
    assert listing.images == ["https://img/xxl.jpg"]


def test_credentials_override_changes_auth_header():
    a = KleinanzeigenAPI(basic_user="u", basic_pw="p")
    b = KleinanzeigenAPI()
    assert a._auth != b._auth
    assert a._auth.startswith("Basic ")


def test_to_dict_roundtrip():
    listing = KleinanzeigenAPI._parse_ad(SAMPLE_AD)
    d = listing.to_dict()
    assert d["id"] == "123456"
    assert d["attributes"]["Zimmer"] == "2"


def test_conversation_to_dict():
    from kleinanzeigen_api import Conversation

    c = Conversation(
        id="c1",
        ad_id="a1",
        ad_title="Titel",
        role="BUYER",
        counterparty="Bob",
        unread=False,
        unread_count=0,
        last_received="2026-07-10",
        preview="Hi",
    )
    assert c.to_dict()["id"] == "c1"


def test_get_ad_and_try_get_ad(monkeypatch):
    api = KleinanzeigenAPI(rate_limit=0)

    class FakeResp:
        def __init__(self, status, data=None):
            self.status_code = status
            self._data = data or {}

        def json(self):
            return self._data

    def fake_get(url, **kw):
        if "999999" in url:
            return FakeResp(404)
        return FakeResp(200, SAMPLE_AD)

    monkeypatch.setattr(api._s, "get", fake_get)
    listing = api.get_ad("123456")
    assert listing.id == "123456"
    assert api.try_get_ad("999999") is None
    assert api._id_exists("123456") is True
    assert api._id_exists("999999") is False
