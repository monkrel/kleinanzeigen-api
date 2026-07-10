"""Offline tests for the logged-in client bits: the post-ad XML builder, chat
parsing, and the auth headers. None of these hit the network."""

from kleinanzeigen_api import KleinanzeigenAPI
from kleinanzeigen_api.client import _parse_conversation


class FakeAuth:
    """Minimal stand-in for an Authenticator, enough for header building."""

    email = "me@x.de"

    def access_token(self):
        return "TOKEN123"


# --- post-ad XML ----------------------------------------------------------- #
def _xml(**over):
    args = dict(
        title="Sofa",
        description="nice sofa",
        category_id=192,
        location_id="3455",
        price=None,
        price_type="FREE",
        poster_type="PRIVATE",
        ad_type="OFFERED",
        contact_name="me",
        email="me@x.de",
        phone=None,
        attributes={},
        picture_urls=[],
        latitude=None,
        longitude=None,
    )
    args.update(over)
    return KleinanzeigenAPI._build_ad_xml(**args)


def test_build_ad_xml_free_has_no_amount():
    xml = _xml(price_type="FREE", price=None)
    assert "<types:value>FREE</types:value>" in xml
    assert "types:amount" not in xml
    assert '<cat:category id="192"/>' in xml
    assert '<loc:location id="3455"/>' in xml
    assert "<ad:email>me@x.de</ad:email>" in xml
    assert '<payment:buy-now selected="false"/>' in xml  # OFFERED ads get this


def test_build_ad_xml_fixed_maps_type_and_keeps_amount():
    xml = _xml(price_type="FIXED", price=120)
    assert "<types:value>SPECIFIED_AMOUNT</types:value>" in xml
    assert "<types:amount>120</types:amount>" in xml


def test_build_ad_xml_negotiable_maps_to_please_contact():
    assert "<types:value>PLEASE_CONTACT</types:value>" in _xml(
        price_type="NEGOTIABLE", price=50
    )


def test_build_ad_xml_escapes_special_characters():
    xml = _xml(title="Tom & Jerry <best>", ad_type="WANTED")
    assert "Tom &amp; Jerry &lt;best&gt;" in xml
    assert "buy-now" not in xml  # only OFFERED ads


def test_build_ad_xml_writes_attributes_and_pictures():
    xml = _xml(attributes={"condition": "used"}, picture_urls=["http://img/1.jpg"])
    assert (
        '<attr:attribute name="condition">'
        "<attr:value>used</attr:value></attr:attribute>"
    ) in xml
    assert 'href="http://img/1.jpg"' in xml


# --- chat parsing ---------------------------------------------------------- #
def test_parse_conversation_counterparty_is_seller_when_buyer():
    conv = _parse_conversation(
        {
            "id": "42",
            "adId": "7",
            "adTitle": "Bike",
            "role": "Buyer",
            "sellerName": "Anna",
            "buyerName": "Me",
            "unread": True,
            "unreadMessagesCount": 3,
            "receivedDate": "2026-06-18T10:00:00Z",
            "textShortTrimmed": "hi",
        }
    )
    assert conv.id == "42" and conv.ad_id == "7"
    assert conv.counterparty == "Anna"
    assert conv.unread is True and conv.unread_count == 3


def test_parse_conversation_counterparty_is_buyer_when_seller():
    conv = _parse_conversation(
        {"id": "1", "role": "Seller", "sellerName": "Me", "buyerName": "Bob"}
    )
    assert conv.counterparty == "Bob"


def test_messages_maps_sent_and_received(monkeypatch):
    api = KleinanzeigenAPI()
    thread = {
        "messages": [
            {"textShort": "hi there", "boundness": "OUTBOUND", "receivedDate": "t1"},
            {"text": "hello", "boundness": "INBOUND", "receivedDate": "t2"},
        ]
    }
    monkeypatch.setattr(api, "conversation", lambda cid: thread)
    msgs = api.messages("x")
    assert msgs[0]["direction"] == "sent" and msgs[0]["text"] == "hi there"
    assert msgs[1]["direction"] == "received" and msgs[1]["text"] == "hello"


# --- auth headers ---------------------------------------------------------- #
def test_headers_gateway_uses_bearer():
    api = KleinanzeigenAPI(authenticator=FakeAuth())
    h = api._headers(authed=True, gateway=True)
    assert h["Authorization"] == "Bearer TOKEN123"
    assert "X-EBAYK-USERID-TOKEN" not in h


def test_headers_main_has_basic_plus_user_tokens():
    api = KleinanzeigenAPI(authenticator=FakeAuth())
    h = api._headers(authed=True)
    assert h["Authorization"].startswith("Basic ")
    assert h["X-EBAYK-USERID-TOKEN"] == "TOKEN123"
    assert h["X-ECG-Authorization-User"] == "email=me@x.de,access=TOKEN123"


def test_headers_without_login_have_no_user_tokens():
    h = KleinanzeigenAPI()._headers()
    assert h["Authorization"].startswith("Basic ")
    assert "X-EBAYK-USERID-TOKEN" not in h


# --- user_id resolution & authenticated actions ---------------------------- #
class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_user_id_lookup_and_cache(monkeypatch):
    api = KleinanzeigenAPI(authenticator=FakeAuth())
    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        return FakeResponse({"data": {"id": 999123}})

    monkeypatch.setattr(api, "_request", fake_request)
    assert api.user_id == "999123"
    assert api.user_id == "999123"  # cached
    assert len(calls) == 1


def test_user_id_no_email_raises():
    import pytest

    class NoEmailAuth:
        email = None

        def access_token(self):
            return "TOKEN"

    api = KleinanzeigenAPI(authenticator=NoEmailAuth())
    with pytest.raises(RuntimeError) as excinfo:
        _ = api.user_id
    assert "no email in the login" in str(excinfo.value)


def test_authenticated_ad_and_chat_actions(monkeypatch):
    api = KleinanzeigenAPI(user_id="12345", authenticator=FakeAuth())
    requests_made = []

    def fake_request(method, url, **kw):
        requests_made.append((method, url, kw))
        if "watchlist.json" in url or "ads.json" in url:
            return FakeResponse(
                {
                    "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ads": {
                        "value": {"ad": [{"id": "10", "title": {"value": "Ad 10"}}]}
                    }
                }
            )
        if "extend/status" in url:
            return FakeResponse([{"id": "10", "eligible": True}])
        if "create-conversation" in url:
            return FakeResponse({"id": "conv-99"})
        return FakeResponse({})

    monkeypatch.setattr(api, "_request", fake_request)

    # Chat actions
    api.reply("conv-1", "Hallo")
    api.mark_read(["conv-1", "conv-2"])
    new_conv = api.start_conversation("ad-50", "Max Mustermann")
    assert new_conv["id"] == "conv-99"

    # Ad actions
    my_ads = api.my_ads(q="test")
    assert len(my_ads) == 1 and my_ads[0].id == "10"
    watched = api.watchlist()
    assert len(watched) == 1 and watched[0].id == "10"

    api.pause_ad("10")
    api.activate_ad("10")
    api.delete_ad("10")
    api.extend_ad("10")
    status = api.extend_status(["10"])
    assert status[0]["eligible"] is True
    assert len(requests_made) >= 9


def test_get_and_request_retries_and_errors(monkeypatch):
    import pytest
    import time

    monkeypatch.setattr(time, "sleep", lambda s: None)
    api = KleinanzeigenAPI(rate_limit=0)
    api.max_retries = 2

    class MockResp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("HTTP Error")

    # Test _get 401 raises clean RuntimeError
    monkeypatch.setattr(api._s, "get", lambda *a, **k: MockResp(401, "unauthorized"))
    with pytest.raises(RuntimeError) as excinfo:
        api._get("http://test")
    assert "Basic-auth credentials likely rotated" in str(excinfo.value)

    # Test _get retries on 500 then succeeds on 200
    responses = [MockResp(500), MockResp(200)]
    monkeypatch.setattr(api._s, "get", lambda *a, **k: responses.pop(0))
    r = api._get("http://test")
    assert r.status_code == 200

    # Test _request 429 retries and exhaust retries
    monkeypatch.setattr(api._s, "request", lambda *a, **k: MockResp(429))
    with pytest.raises(RuntimeError) as excinfo:
        api._request("POST", "http://test", data="xml", authed=False)
    assert "failed after 2 tries" in str(excinfo.value)


def test_location_helpers(monkeypatch):
    import pytest

    api = KleinanzeigenAPI(rate_limit=0)

    class MockWebResp:
        def json(self):
            return {"_3331": "Berlin", "_0": "All"}

    monkeypatch.setattr(api._s, "get", lambda *a, **k: MockWebResp())
    res = api._resolve_location_web("Ber")
    assert res == [("3331", "Berlin")]

    assert api._location_to_id(3331) == "3331"
    assert api._location_to_id("3331") == "3331"

    monkeypatch.setattr(api, "best_location", lambda q: None)
    with pytest.raises(ValueError) as excinfo:
        api._location_to_id("InvalidCityXYZ")
    assert "Could not resolve location" in str(excinfo.value)
