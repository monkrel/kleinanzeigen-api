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
    args = dict(title="Sofa", description="nice sofa", category_id=192,
                location_id="3455", price=None, price_type="FREE",
                poster_type="PRIVATE", ad_type="OFFERED", contact_name="me",
                email="me@x.de", phone=None, attributes={}, picture_urls=[],
                latitude=None, longitude=None)
    args.update(over)
    return KleinanzeigenAPI._build_ad_xml(**args)


def test_build_ad_xml_free_has_no_amount():
    xml = _xml(price_type="FREE", price=None)
    assert "<types:value>FREE</types:value>" in xml
    assert "types:amount" not in xml
    assert '<cat:category id="192"/>' in xml
    assert '<loc:location id="3455"/>' in xml
    assert "<ad:email>me@x.de</ad:email>" in xml
    assert '<payment:buy-now selected="false"/>' in xml   # OFFERED ads get this


def test_build_ad_xml_fixed_maps_type_and_keeps_amount():
    xml = _xml(price_type="FIXED", price=120)
    assert "<types:value>SPECIFIED_AMOUNT</types:value>" in xml
    assert "<types:amount>120</types:amount>" in xml


def test_build_ad_xml_negotiable_maps_to_please_contact():
    assert "<types:value>PLEASE_CONTACT</types:value>" in _xml(price_type="NEGOTIABLE",
                                                               price=50)


def test_build_ad_xml_escapes_special_characters():
    xml = _xml(title="Tom & Jerry <best>", ad_type="WANTED")
    assert "Tom &amp; Jerry &lt;best&gt;" in xml
    assert "buy-now" not in xml   # only OFFERED ads


def test_build_ad_xml_writes_attributes_and_pictures():
    xml = _xml(attributes={"condition": "used"}, picture_urls=["http://img/1.jpg"])
    assert ('<attr:attribute name="condition">'
            "<attr:value>used</attr:value></attr:attribute>") in xml
    assert 'href="http://img/1.jpg"' in xml


# --- chat parsing ---------------------------------------------------------- #
def test_parse_conversation_counterparty_is_seller_when_buyer():
    conv = _parse_conversation({
        "id": "42", "adId": "7", "adTitle": "Bike", "role": "Buyer",
        "sellerName": "Anna", "buyerName": "Me", "unread": True,
        "unreadMessagesCount": 3, "receivedDate": "2026-06-18T10:00:00Z",
        "textShortTrimmed": "hi"})
    assert conv.id == "42" and conv.ad_id == "7"
    assert conv.counterparty == "Anna"
    assert conv.unread is True and conv.unread_count == 3


def test_parse_conversation_counterparty_is_buyer_when_seller():
    conv = _parse_conversation({"id": "1", "role": "Seller",
                                "sellerName": "Me", "buyerName": "Bob"})
    assert conv.counterparty == "Bob"


def test_messages_maps_sent_and_received(monkeypatch):
    api = KleinanzeigenAPI()
    thread = {"messages": [
        {"textShort": "hi there", "boundness": "OUTBOUND", "receivedDate": "t1"},
        {"text": "hello", "boundness": "INBOUND", "receivedDate": "t2"}]}
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
