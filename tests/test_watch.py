"""Offline tests for the new-ad watcher (iter_new_ads / current_max_id).

No network: we replace _fetch_ad_json (frontier mode) or search_page (search
mode) with fakes, so the frontier walk, the 404-retry queue, the cache-dodging
size rotation and the client-side filters can be checked deterministically.
"""

import itertools

from kleinanzeigen_api import KleinanzeigenAPI, Listing
from kleinanzeigen_api.client import _haversine_km


def _ad_json(
    ad_id,
    *,
    title="thing",
    price=None,
    price_type="SPECIFIED_AMOUNT",
    category="1",
    poster="PRIVATE",
    lat=None,
    lon=None,
):
    """Build a minimal single-ad payload the way the API returns it."""
    price_block = {"price-type": {"value": price_type}}
    if price is not None:
        price_block["amount"] = {"value": price}
    addr = {"state": {"value": "Berlin"}, "zip-code": {"value": "10115"}}
    if lat is not None:
        addr["latitude"] = {"value": lat}
    if lon is not None:
        addr["longitude"] = {"value": lon}
    return {
        "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad": {
            "value": {
                "id": ad_id,
                "title": {"value": title},
                "description": {"value": ""},
                "price": price_block,
                "ad-address": addr,
                "poster-type": {"value": poster},
                "category": {"id": category},
            }
        }
    }


def _fake_api(existing: dict):
    """A client whose _fetch_ad_json reads from an in-memory {id: payload} map."""
    api = KleinanzeigenAPI(rate_limit=0)
    api._throttle = lambda: None  # no sleeping in tests
    api._fetch_ad_json = lambda ad_id: existing.get(int(ad_id))
    return api


def _listing(ad_id, title="thing", posted=""):
    return Listing(
        id=str(ad_id),
        title=title,
        description="",
        price=None,
        price_type="",
        url="",
        city="",
        zip_code="",
        latitude=None,
        longitude=None,
        size_m2=None,
        rooms=None,
        posted=posted,
        poster_type="",
    )


def _fake_search_api(pages):
    """A client whose search_page pops results from a list of prepared pages.

    Also records the size= of every call so tests can check the rotation.
    """
    api = KleinanzeigenAPI(rate_limit=0)
    api._throttle = lambda: None
    api.sizes_used = []
    pages = list(pages)

    def fake_search_page(**kwargs):
        api.sizes_used.append(kwargs.get("size"))
        page = pages.pop(0) if len(pages) > 1 else pages[0]
        return len(page), page

    api.search_page = fake_search_page
    return api


def test_haversine_known_distance():
    # Berlin center -> ~9 km away point, roughly
    d = _haversine_km(52.52, 13.405, 52.52, 13.405)
    assert d == 0.0
    d2 = _haversine_km(52.52, 13.405, 52.60, 13.405)
    assert 8 < d2 < 10  # ~8.9 km per 0.08° of latitude


def test_current_max_id_finds_frontier():
    ids = {i: _ad_json(i) for i in range(1000, 1051)}  # frontier at 1050
    api = _fake_api(ids)
    assert api.current_max_id(seed=1000) == 1050


def test_current_max_id_ignores_deleted_gaps():
    # dense space with holes (deleted ads) but a real frontier at 1200
    ids = {i: _ad_json(i) for i in range(1000, 1201) if i % 7}  # ~1/7 missing
    api = _fake_api(ids)
    assert api.current_max_id(seed=1000) == 1200


def test_iter_new_ads_yields_only_new_and_stops_at_cap():
    ids = {i: _ad_json(i) for i in range(1000, 1011)}
    api = _fake_api(ids)
    # start just below the frontier; take the two newest
    out = list(
        itertools.islice(
            api.iter_new_ads(mode="frontier", start_id=1008, poll_interval=0), 2
        )
    )
    assert [listing.id for listing in out] == ["1009", "1010"]


def test_iter_new_ads_filters():
    ids = {
        1001: _ad_json(1001, price_type="FREE", category="80", title="sofa gratis"),
        1002: _ad_json(1002, price=50, category="80", title="sofa"),
        1003: _ad_json(1003, price_type="FREE", category="99", title="lamp"),
        1004: _ad_json(
            1004,
            price_type="FREE",
            category="80",
            title="bike frei",
            lat=52.52,
            lon=13.40,
        ),
    }
    api = _fake_api(ids)

    free = list(
        itertools.islice(
            api.iter_new_ads(
                mode="frontier", start_id=1000, price_type="FREE", poll_interval=0
            ),
            3,
        )
    )
    assert [listing.id for listing in free] == ["1001", "1003", "1004"]

    free_cat80 = list(
        itertools.islice(
            api.iter_new_ads(
                mode="frontier",
                start_id=1000,
                price_type="FREE",
                category_id="80",
                poll_interval=0,
            ),
            2,
        )
    )
    assert [listing.id for listing in free_cat80] == ["1001", "1004"]

    near = list(
        itertools.islice(
            api.iter_new_ads(
                mode="frontier",
                start_id=1000,
                near=(52.52, 13.40),
                radius_km=5,
                poll_interval=0,
            ),
            1,
        )
    )
    assert [listing.id for listing in near] == ["1004"]  # only ad with coords in range


def test_frontier_retries_missing_ids():
    # 1005 is a hole on the first pass (say, still being checked by the site)
    # and shows up a bit later. The watcher must come back for it.
    ids = {i: _ad_json(i) for i in range(1000, 1011) if i != 1005}
    api = _fake_api(ids)
    seen = []

    gen = api.iter_new_ads(mode="frontier", start_id=1004, poll_interval=0)
    # first pass: 1005 is missing, 1006-1010 come through
    for listing in itertools.islice(gen, 5):
        seen.append(listing.id)
    assert seen == ["1006", "1007", "1008", "1009", "1010"]

    # now the held-back ad appears; the next round should pick it up
    ids[1005] = _ad_json(1005, title="late one")
    late = next(gen)
    assert late.id == "1005"


def test_frontier_gives_up_on_old_missing_ids():
    # with retry_missing_for=0 a hole is dropped right away and never yielded
    ids = {i: _ad_json(i) for i in range(1000, 1011) if i != 1005}
    api = _fake_api(ids)
    gen = api.iter_new_ads(
        mode="frontier", start_id=1004, poll_interval=0, retry_missing_for=0
    )
    assert [listing.id for listing in itertools.islice(gen, 5)] == [
        "1006",
        "1007",
        "1008",
        "1009",
        "1010",
    ]
    ids[1005] = _ad_json(1005)  # too late, it was already dropped
    ids[1011] = _ad_json(1011)  # give the generator something new to yield
    assert next(gen).id == "1011"
    # if 1005 were still in the retry queue it would come out before 1012
    # (retries run before the forward walk) - it must not
    ids[1012] = _ad_json(1012)
    assert next(gen).id == "1012"


def test_search_mode_yields_only_new_ads():
    page1 = [_listing(3), _listing(2), _listing(1)]  # newest first
    page2 = [_listing(5), _listing(4), _listing(3), _listing(2)]
    api = _fake_search_api([page1, page2])

    gen = api.iter_new_ads(mode="search", poll_interval=0)
    out = [listing.id for listing in itertools.islice(gen, 2)]
    # page1 is the baseline (not yielded), only the genuinely new 4 and 5 come
    # out, oldest first
    assert out == ["4", "5"]


def test_search_mode_backfill_yields_first_page_too():
    page1 = [_listing(2), _listing(1)]
    api = _fake_search_api([page1])
    gen = api.iter_new_ads(mode="search", backfill=True, poll_interval=0)
    assert [listing.id for listing in itertools.islice(gen, 2)] == ["1", "2"]


def test_search_mode_rotates_page_size():
    # the whole point of search mode: never repeat a page size within a short
    # window, so we never get the site's cached answer. One new ad per page
    # lets islice drive the generator through as many polls as we like.
    pages = [[_listing(i)] for i in range(1, 31)]
    api = _fake_search_api(pages)
    gen = api.iter_new_ads(mode="search", poll_interval=0)
    list(itertools.islice(gen, 25))  # baseline page + 25 polls
    sizes = api.sizes_used
    assert all(25 <= s <= 100 for s in sizes)
    assert len(set(sizes)) == len(sizes)  # all different -> no cache hits


def test_search_mode_skips_ads_older_than_start():
    # later polls use bigger page sizes and reach further into the past;
    # ads from before we started watching must not come out as "new"
    page1 = [_listing(10, posted="2026-07-05T12:00:00.000+0200")]
    page2 = [
        _listing(11, posted="2026-07-05T12:01:00.000+0200"),  # genuinely new
        _listing(10, posted="2026-07-05T12:00:00.000+0200"),
        _listing(3, posted="2026-07-05T11:30:00.000+0200"),  # old, deeper page
    ]
    api = _fake_search_api([page1, page2])
    gen = api.iter_new_ads(mode="search", poll_interval=0)
    assert next(gen).id == "11"  # the old id-3 ad was skipped


def test_search_mode_rejects_start_id():
    api = KleinanzeigenAPI(rate_limit=0)
    try:
        api.iter_new_ads(mode="search", start_id=5)
    except ValueError:
        pass
    else:
        raise AssertionError("start_id in search mode should raise")


def test_frontier_mode_rejects_location():
    api = KleinanzeigenAPI(rate_limit=0)
    try:
        api.iter_new_ads(mode="frontier", location="Berlin")
    except ValueError:
        pass
    else:
        raise AssertionError("location in frontier mode should raise")
