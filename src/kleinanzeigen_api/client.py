"""Unofficial Kleinanzeigen JSON API client.

This calls the real mobile app REST API (api.kleinanzeigen.de) instead of
scraping the website, which is what most similar tools do. It returns structured
data the website doesn't show: GPS coordinates, exact result counts, typed
attributes (Wohnfläche, Zimmer, Nebenkosten, Warmmiete, Kaution, booleans like
pets_allowed), all image sizes, ISO timestamps and the price type.

How the requests work:
  - The app uses HTTP Basic auth plus an X-EBAYK-APP install id. The app builds
    that id on the client side (a UUID plus a 13-digit millisecond timestamp),
    so we can just build our own.
  - curl_cffi copies a real Chrome TLS fingerprint so the CDN lets the request
    through. A normal requests client is blocked at the TLS layer.

The Basic-auth login is shipped inside the Android app, so it is not a personal
secret, but it can change. If you start getting 401/403 responses, pass new
values with the basic_user / basic_pw arguments or the KLEINANZEIGEN_BASIC_USER
/ KLEINANZEIGEN_BASIC_PW environment variables.

Kleinanzeigen's terms of service forbid automation, so keep this for personal
use and don't send requests too fast.
"""

from __future__ import annotations

import base64
import html
import os
import random
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from curl_cffi import requests as creq

from . import categories as _catalog

API_HOST = "https://api.kleinanzeigen.de"
# chat and a few other things live on a separate "gateway" host
GATEWAY_HOST = "https://gateway.kleinanzeigen.de"
WEB_HOST = "https://www.kleinanzeigen.de"
ADS_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ads"
LOCATIONS_NS = "{http://www.ebayclassifiedsgroup.com/schema/location/v1}locations"
SEARCH_META_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ads-search-options"

# --- baked-in app-distribution values (override via env / constructor) -------
APP_VERSION = "2026.25.0"
DEFAULT_BASIC_USER = "android"
DEFAULT_BASIC_PW = "TaR60pEttY"

CATEGORY_WOHNUNG_MIETEN = 203

# the list of fields we ask for on ad lists. Without it the API only returns a
# few basic fields, so we send the same long list the app uses to get everything
# (images, labels, ad status, etc.).
ADS_FIELD_SELECTOR = (
    "id,title,description,displayoptions,start-date-time,category.id,"
    "category.localized_name,ad-address.state,ad-address.zip-code,"
    "ad-address.availability-radius-in-km,price,pictures,link,features-active,"
    "search-distance,negotiation-enabled,attributes,medias,medias.media,"
    "medias.media.title,medias.media.media-link,buy-now,placeholder-image-present,"
    "labels,price-reduction,company,embedded,poster-type,seller-account-type,"
    "ad-status,ad-address.latitude,ad-address.longitude,locations,user-id,"
    "repost-url,ad-type"
)


# --------------------------------------------------------------------------- #
# capi (eBay-Classifieds) JSON helpers — values are wrapped in {"value": ...}
# --------------------------------------------------------------------------- #
def _val(node):
    """Pull the scalar value out of a capi node. Handles nesting like {'value': {'value': x}}."""
    if isinstance(node, dict):
        if "value" in node:
            return _val(node["value"])
        return node
    return node


def _num(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _as_terms(exclude) -> list:
    """Turn the exclude argument (str, list, or None) into a list of lowercase terms."""
    if not exclude:
        return []
    if isinstance(exclude, str):
        exclude = [exclude]
    return [str(t).strip().lower() for t in exclude if t and str(t).strip()]


def _excluded(listing, terms) -> bool:
    """Return True if the title or description contains any of the excluded terms."""
    if not terms:
        return False
    hay = f"{listing.title}\n{listing.description}".lower()
    return any(t in hay for t in terms)


def _posted_dt(posted: str):
    """Parse a Listing.posted timestamp, or None if it won't parse.

    The API sends offsets like "+0200" without a colon, which older Pythons
    can't read, so we patch the colon in first.
    """
    import datetime

    s = posted or ""
    if len(s) > 5 and s[-5] in "+-" and ":" not in s[-5:]:
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km between two lat/lon points."""
    import math

    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Listing:
    """One ad returned by the API."""

    id: str
    title: str
    description: str
    price: Optional[float]
    price_type: str
    url: str
    city: str
    zip_code: str
    latitude: Optional[float]
    longitude: Optional[float]
    size_m2: Optional[float]
    rooms: Optional[float]
    posted: str
    poster_type: str
    category_id: str = ""
    images: list = field(default_factory=list)
    attributes: dict = field(default_factory=dict)  # localized-label -> value

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Conversation:
    """One chat thread from your inbox."""

    id: str
    ad_id: str
    ad_title: str
    role: str  # "BUYER" or "SELLER"
    counterparty: str  # name of the other person
    unread: bool
    unread_count: int
    last_received: str
    preview: str
    raw: dict = field(default_factory=dict)  # the full API object, just in case

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_conversation(c: dict) -> "Conversation":
    role = (c.get("role") or "").upper()
    # the counterparty is the *other* side relative to your role
    other = c.get("sellerName") if role == "BUYER" else c.get("buyerName")
    return Conversation(
        id=str(c.get("id", "")),
        ad_id=str(c.get("adId", "")),
        ad_title=c.get("adTitle") or "",
        role=role,
        counterparty=other or c.get("sellerName") or c.get("buyerName") or "",
        unread=bool(c.get("unread")),
        unread_count=int(c.get("unreadMessagesCount") or 0),
        last_received=c.get("receivedDate") or "",
        preview=c.get("textShortTrimmed") or "",
        raw=c,
    )


def _message_text(m: dict) -> str:
    return m.get("text") or m.get("textShort") or m.get("title") or ""


class KleinanzeigenAPI:
    """Client for the Kleinanzeigen mobile JSON API.

    Arguments:
        rate_limit: minimum seconds to wait between requests (plus a little
            random jitter).
        app_version: version string sent in the app headers.
        timeout: per-request timeout in seconds.
        max_retries: how many times to retry on temporary errors (429, 5xx, or
            network problems).
        basic_user / basic_pw: override the built-in Basic-auth login. If these
            are not set, the KLEINANZEIGEN_BASIC_USER / KLEINANZEIGEN_BASIC_PW
            environment variables are used, then the built-in defaults.
        authenticator: an auth.Authenticator if you want the logged-in features
            (chat, your own ads, posting). Leave it out for plain search.
        user_id: your numeric account id. Optional - we look it up from your
            login when it's first needed.
    """

    def __init__(
        self,
        rate_limit: float = 1.5,
        app_version: str = APP_VERSION,
        timeout: int = 25,
        max_retries: int = 3,
        basic_user: Optional[str] = None,
        basic_pw: Optional[str] = None,
        authenticator=None,
        user_id: Optional[str] = None,
    ):
        self.rate_limit = rate_limit
        self.app_version = app_version
        self.timeout = timeout
        self.max_retries = max_retries
        self._last = 0.0
        # build one install id per client, the same way the app makes one per install
        self._xapp = f"{uuid.uuid4()}{int(time.time() * 1000)}"
        user = basic_user or os.getenv("KLEINANZEIGEN_BASIC_USER") or DEFAULT_BASIC_USER
        pw = basic_pw or os.getenv("KLEINANZEIGEN_BASIC_PW") or DEFAULT_BASIC_PW
        self._auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        self._s = creq.Session(impersonate="chrome")
        # optional logged-in user (for chat / own ads / posting). authenticator is
        # an auth.Authenticator; user_id is the numeric account id (resolved lazily
        # from the login email if not given).
        self._auth_provider = authenticator
        self._user_id = str(user_id) if user_id is not None else None

    # -- transport ---------------------------------------------------------- #
    def _headers(self, authed: bool = False, gateway: bool = False) -> dict:
        h = {
            "X-EBAYK-APP": self._xapp,
            "X-ECG-USER-AGENT": f"ebayk-android-app-{self.app_version}",
            "X-ECG-USER-VERSION": self.app_version,
            "User-Agent": f"Kleinanzeigen/{self.app_version} (Android 13; Pixel 7)",
            "Accept": "application/json",
            "Accept-Language": "de-DE",
        }
        if gateway:
            # the gateway host wants a normal "Bearer <token>" header
            if authed:
                h["Authorization"] = "Bearer " + self._require_auth().access_token()
        else:
            # main API: Basic auth, and for logged-in calls two extra user headers.
            h["Authorization"] = self._auth
            if authed:
                auth = self._require_auth()
                token = auth.access_token()
                h["X-EBAYK-USERID-TOKEN"] = token
                if auth.email:
                    h["X-ECG-Authorization-User"] = f"email={auth.email},access={token}"
        return h

    def _require_auth(self):
        if self._auth_provider is None:
            raise RuntimeError(
                "This call needs a logged-in user. Create an "
                "auth.Authenticator(), log in once, and pass it as "
                "KleinanzeigenAPI(authenticator=...)."
            )
        return self._auth_provider

    def _throttle(self):
        wait = self.rate_limit - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))

    def _get(self, url: str, params: Optional[dict] = None) -> creq.Response:
        last = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                r = self._s.get(
                    url, params=params, headers=self._headers(), timeout=self.timeout
                )
                self._last = time.time()
                if r.status_code == 200:
                    return r
                if r.status_code in (401, 403):
                    raise RuntimeError(
                        f"{r.status_code} from API — Basic-auth credentials likely "
                        f"rotated. Supply fresh ones via basic_user/basic_pw or the "
                        f"KLEINANZEIGEN_BASIC_USER/KLEINANZEIGEN_BASIC_PW env vars. "
                        f"Body: {r.text[:160]}"
                    )
                if r.status_code in (429, 500, 503):
                    time.sleep(1.5 * attempt + random.uniform(0, 1.5))
                    continue
                r.raise_for_status()
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001 - retry on any network error
                last = e
                self._last = time.time()
                time.sleep(1.2 * attempt)
        raise RuntimeError(f"GET failed after {self.max_retries} tries: {url} ({last})")

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        data: Optional[str] = None,
        content_type: Optional[str] = None,
        authed: bool = True,
        gateway: bool = False,
    ) -> creq.Response:
        """Send a POST/PUT/DELETE. Used by the logged-in chat and ad calls.

        Retries on the same temporary errors as _get. On other errors it raises
        with the response body so you can see what went wrong (a 403 usually
        means the login token is missing or expired). Use gateway=True for the
        gateway host, and data + content_type when you need to send a raw body
        like the post-ad XML
        """
        last = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                headers = self._headers(authed=authed, gateway=gateway)
                if content_type:
                    headers["Content-Type"] = content_type
                body = data.encode("utf-8") if isinstance(data, str) else data
                r = self._s.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=body,
                    headers=headers,
                    timeout=self.timeout,
                )
                self._last = time.time()
                if r.status_code in (200, 201, 204):
                    return r
                if r.status_code in (429, 500, 503):
                    time.sleep(1.5 * attempt + random.uniform(0, 1.5))
                    continue
                raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001 - retry on any network error
                last = e
                self._last = time.time()
                time.sleep(1.2 * attempt)
        raise RuntimeError(
            f"{method} failed after {self.max_retries} tries: {url} ({last})"
        )

    # -- location resolution ------------------------------------------------ #
    def resolve_location(self, query: str) -> list:
        """Look up a place name and return a list of (location_id, label) matches.

        Asks the app's location endpoint first. If that call fails for any reason
        (e.g. the credentials stopped working), it quietly tries the website
        instead, so this keeps working either way.
        """
        try:
            cands = self._resolve_location_api(query)
            if cands:
                return cands
        except Exception:  # API down or response unreadable -> try the website
            pass
        return self._resolve_location_web(query)

    def _resolve_location_api(self, query: str) -> list:
        """Look up a place name using the app's /api/locations.json endpoint."""
        data = self._get(f"{API_HOST}/api/locations.json", params={"q": query}).json()
        root = _val(data.get(LOCATIONS_NS, {}))
        nodes = root.get("location") if isinstance(root, dict) else None
        if isinstance(nodes, dict):  # one match comes back as a single item, not a list
            nodes = [nodes]
        out: list = []
        self._flatten_locations(nodes, out)
        return out

    def _flatten_locations(self, nodes, out: list) -> None:
        """Turn the nested location tree into a flat list of (id, label) pairs.

        A place can contain sub-places (Berlin -> Charlottenburg -> a postcode).
        We add each place before its sub-places, so bigger areas like "Berlin"
        end up first and best_location() picks them over smaller ones.
        """
        for n in nodes or []:
            lid = _val(n.get("id"))
            label = _val(n.get("localized-name")) or _val(n.get("id-name"))
            if lid is not None and label:
                out.append((str(lid), label))
            kids = n.get("location")
            if kids:
                self._flatten_locations(kids if isinstance(kids, list) else [kids], out)

    def _resolve_location_web(self, query: str) -> list:
        """Look up a place name on the website (used only as a backup)."""
        r = self._s.get(
            f"{WEB_HOST}/s-ort-empfehlungen.json",
            params={"query": query},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept-Language": "de-DE"},
            timeout=self.timeout,
        )
        out = []
        for k, label in r.json().items():
            lid = k.lstrip("_")
            if lid != "0":
                out.append((lid, label))
        return out

    def _location_to_id(self, location) -> str:
        """Turn a place name (or an id that's already numeric) into a location id."""
        if str(location).isdigit():
            return str(location)
        best = self.best_location(location)
        if not best:
            raise ValueError(
                f"Could not resolve location {location!r}. Check the spelling "
                f"with resolve_location(), pass a numeric location id, or use "
                f"location=None to search all of Germany."
            )
        return best[0]

    def best_location(self, query: str) -> Optional[tuple]:
        """Return the single best (location_id, label) guess for a place name, or None."""
        cands = self.resolve_location(query)
        if not cands:
            return None
        ql = query.lower()
        # Match the name exactly, ignoring the region part that may follow it,
        # e.g. "Berlin - Berlin" (website) or "Charlottenburg (Berlin)" (app).
        for lid, label in cands:
            head = label.split(" - ")[0].split(" (")[0].strip().lower()
            if head == ql:
                return lid, label
        for lid, label in cands:
            if "-" not in label and label.lower().startswith(ql):
                return lid, label
        return cands[0]

    # -- parsing ------------------------------------------------------------ #
    @staticmethod
    def _parse_ad(ad: dict) -> Listing:
        """Build a Listing from one ad dict in the API response."""
        addr = ad.get("ad-address", {})
        price = ad.get("price", {})
        # attributes
        attrs, size, rooms = {}, None, None
        for at in (ad.get("attributes", {}) or {}).get("attribute", []) or []:
            label = at.get("localized-label") or at.get("name")
            vlist = at.get("value") or []
            v = vlist[0].get("value") if vlist else None
            attrs[label] = v
            name = at.get("name", "")
            if name.endswith(".qm"):  # wohnung_mieten.qm / haus_mieten.qm
                size = _num(v)
            elif name.endswith(".zimmer"):  # *_mieten.zimmer
                rooms = _num(v)
        # public website link
        url = ""
        for ln in ad.get("link", []) or []:
            if ln.get("rel") == "self-public-website":
                url = ln.get("href", "")
        # images: collect the image urls, preferring the larger sizes
        images = []
        for pic in (ad.get("pictures", {}) or {}).get("picture", []) or []:
            best = None
            for ln in pic.get("link", []) or []:
                href = ln.get("href", "")
                if ln.get("rel") in ("XXL", "large", "teaser") or best is None:
                    best = href
            if best:
                images.append(best)
        return Listing(
            id=str(ad.get("id", "")),
            title=html.unescape(_val(ad.get("title")) or ""),
            description=html.unescape(_val(ad.get("description")) or ""),
            price=_num(_val(price.get("amount"))) if price.get("amount") else None,
            price_type=_val(price.get("price-type")) or "",
            url=url,
            city=_val(addr.get("state")) or "",
            zip_code=_val(addr.get("zip-code")) or "",
            latitude=_num(_val(addr.get("latitude"))),
            longitude=_num(_val(addr.get("longitude"))),
            size_m2=size,
            rooms=rooms,
            posted=_val(ad.get("start-date-time")) or "",
            poster_type=_val(ad.get("poster-type")) or "",
            category_id=str(_val((ad.get("category") or {}).get("id")) or ""),
            images=images,
            attributes=attrs,
        )

    # -- search ------------------------------------------------------------- #
    def search_page(
        self,
        *,
        category_id=None,
        location_id=None,
        distance_km=None,
        min_price=None,
        max_price=None,
        ad_type="OFFERED",
        q=None,
        picture_required=False,
        sort_type=None,
        page=0,
        size=25,
    ) -> tuple:
        """Fetch one page of results. Returns (total_found, list_of_Listing)."""
        params = {"page": page, "size": size}
        if category_id:
            params["categoryId"] = category_id
        if location_id:
            params["locationId"] = location_id
        if distance_km is not None:
            params["distance"] = distance_km
        if min_price is not None:
            params["minPrice"] = min_price
        if max_price is not None:
            params["maxPrice"] = max_price
        if ad_type:
            params["adType"] = ad_type
        if q:
            params["q"] = q
        if picture_required:
            params["pictureRequired"] = "true"
        if (
            sort_type
        ):  # PRICE_ASCENDING | PRICE_DESCENDING | DATE_DESCENDING | DISTANCE_ASCENDING
            params["sortType"] = sort_type

        data = self._get(f"{API_HOST}/api/ads.json", params=params).json()
        return self._parse_ads_block(data)

    def _parse_ads_block(self, data: dict) -> tuple:
        """Turn an ads-list response into (total_found, [Listing]).

        Search, your own ads and the watchlist all come back in the same shape,
        so they all use this.
        """
        block = data.get(ADS_NS, {}).get("value", {})
        total = int(_num(block.get("paging", {}).get("numFound")) or 0)
        raw = block.get("ad", [])
        if isinstance(
            raw, dict
        ):  # a single result comes back as one object, not a list
            raw = [raw]
        return total, [self._parse_ad(a) for a in raw]

    def search(
        self,
        location=None,
        *,
        q=None,
        exclude=None,
        category=None,
        category_id=None,
        distance_km=None,
        min_price=None,
        max_price=None,
        min_rooms=None,
        max_rooms=None,
        min_size=None,
        max_size=None,
        ad_type="OFFERED",
        sort_type=None,
        pages=1,
        size=25,
        sort_by_price=False,
    ) -> list:
        """Search kleinanzeigen.de. By default this searches every category.

        Picking a category:
          - category takes a name or an id, for example "Fahrräder & Zubehör" or
            217. Names are looked up in the bundled catalog and raise a
            ValueError if they are unknown or match more than one category (use
            find_categories() to browse).
          - category_id is the same thing but only accepts an id. Pass either
            category or category_id, not both.

        location can be a city/region name (looked up automatically) or a numeric
        id. q is the keyword sent to the server. exclude (a string or list of
        strings) removes any result whose title or description contains one of
        the terms (case-insensitive, done on our side). min_rooms / max_rooms /
        min_size / max_size filter real-estate ads on our side and are ignored
        for ads that don't have those values.

        Returns a list of Listing, in the order the server sorted them
        (sort_type).
        """
        if category is not None and category_id is not None:
            raise ValueError("pass either `category` or `category_id`, not both")
        category_id = _catalog.resolve_category(
            category if category is not None else category_id
        )
        if sort_by_price and not sort_type:  # default to cheapest-first
            sort_type = "PRICE_ASCENDING"
        exclude_terms = _as_terms(exclude)
        location_id = self._location_to_id(location) if location else None

        results, seen = [], set()
        for page in range(pages):
            total, listings = self.search_page(
                category_id=category_id,
                location_id=location_id,
                distance_km=distance_km,
                min_price=min_price,
                max_price=max_price,
                ad_type=ad_type,
                q=q,
                sort_type=sort_type,
                page=page,
                size=size,
            )
            if not listings:
                break
            for listing in listings:
                if listing.id in seen:
                    continue
                if _excluded(listing, exclude_terms):
                    continue
                if min_rooms is not None and (
                    listing.rooms is None or listing.rooms < min_rooms
                ):
                    continue
                if max_rooms is not None and (
                    listing.rooms is None or listing.rooms > max_rooms
                ):
                    continue
                if min_size is not None and (
                    listing.size_m2 is None or listing.size_m2 < min_size
                ):
                    continue
                if max_size is not None and (
                    listing.size_m2 is None or listing.size_m2 > max_size
                ):
                    continue
                seen.add(listing.id)
                results.append(listing)
            if (page + 1) * size >= total:
                break
        return results  # already ordered by the server (sort_type)

    def search_rentals(self, location=None, **kwargs) -> list:
        """Shortcut for search() limited to apartment rentals (category id 203,
        "Mietwohnungen"). Takes the same keyword arguments as search(); pass
        category_id yourself to use a different category.
        """
        kwargs.setdefault("category_id", CATEGORY_WOHNUNG_MIETEN)
        return self.search(location, **kwargs)

    def search_metadata(self, category=None, *, category_id=None) -> dict:
        """List the filters you can search a category with.

        Returns a dict like ``param_name -> {label, type, search_param, values}``.
        ``values`` only appears for filters with a fixed set of choices (e.g.
        priceType, adType) and holds the allowed ``(value, label)`` pairs. This
        is the same filter info the app uses to draw its filter screen.

        Give a category as a name or id via ``category``, or an id via
        ``category_id``; one of them is required.
        """
        if category is not None and category_id is not None:
            raise ValueError("pass either `category` or `category_id`, not both")
        cat = _catalog.resolve_category(
            category if category is not None else category_id
        )
        if cat is None:
            raise ValueError("search_metadata needs a category (name or id)")
        data = self._get(f"{API_HOST}/api/ads/search-metadata/{cat}.json").json()
        opts = _val(data.get(SEARCH_META_NS, {}))
        out: dict = {}
        for name, spec in opts.items() if isinstance(opts, dict) else []:
            if not isinstance(spec, dict):
                continue
            entry = {
                "label": spec.get("localized-label"),
                "type": spec.get("type"),
                "search_param": spec.get("search-param"),
            }
            sv = spec.get("supported-value")
            if sv:
                if isinstance(sv, dict):  # one choice comes back alone, not in a list
                    sv = [sv]
                entry["values"] = [
                    (v.get("value"), v.get("localized-label"))
                    for v in sv
                    if isinstance(v, dict)
                ]
            out[name] = entry
        return out

    def _fetch_ad_json(self, ad_id) -> Optional[dict]:
        """GET one ad by id. Returns the parsed body, or None if it 404s.

        Unlike _get, a 404 here is an expected, cheap "no such ad" (used heavily
        by the frontier watcher to probe ids that don't exist yet), so we don't
        burn the retry budget on it. Transient errors (429/5xx/network) still
        retry the same way _get does.
        """
        last = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                r = self._s.get(
                    f"{API_HOST}/api/ads/{ad_id}.json",
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                self._last = time.time()
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                if r.status_code in (401, 403):
                    raise RuntimeError(
                        f"{r.status_code} from API — Basic-auth credentials likely "
                        f"rotated. Supply fresh ones via basic_user/basic_pw or the "
                        f"KLEINANZEIGEN_BASIC_USER/KLEINANZEIGEN_BASIC_PW env vars."
                    )
                if r.status_code in (429, 500, 503):
                    time.sleep(1.5 * attempt + random.uniform(0, 1.5))
                    continue
                r.raise_for_status()
            except RuntimeError:
                raise
            except Exception as e:  # noqa: BLE001 - retry on any network error
                last = e
                self._last = time.time()
                time.sleep(1.2 * attempt)
        raise RuntimeError(
            f"GET ad {ad_id} failed after {self.max_retries} tries ({last})"
        )

    @staticmethod
    def _unwrap_ad(data: dict) -> dict:
        """Pull the ad object out of a single-ad payload (wrapped under an "ad" key)."""
        ad = data.get("{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad", data)
        return ad.get("value", ad) if isinstance(ad, dict) else ad

    def get_ad(self, ad_id: str) -> Listing:
        """Fetch a single ad by id. Raises if the ad does not exist."""
        data = self._fetch_ad_json(ad_id)
        if data is None:
            raise RuntimeError(f"ad {ad_id} not found (404)")
        return self._parse_ad(self._unwrap_ad(data))

    def try_get_ad(self, ad_id) -> Optional[Listing]:
        """Fetch a single ad by id, or return None if it doesn't exist (404).

        Unlike search, the single-ad endpoint has no result cache — a brand-new
        ad is readable here within seconds of being posted, minutes before a
        plain search would show it. iter_new_ads' frontier mode builds on this.
        """
        data = self._fetch_ad_json(ad_id)
        return self._parse_ad(self._unwrap_ad(data)) if data is not None else None

    # -- near-real-time watching (bypasses the lagged search index) --------- #
    def _id_exists(self, ad_id) -> bool:
        return self._fetch_ad_json(ad_id) is not None

    def _beyond_max(self, ad_id) -> bool:
        """True if ad_id looks past the live frontier, not just a deleted-ad gap.

        ~6% of ids below the current maximum 404 because the ad was deleted or
        removed by moderation, so a single 404 doesn't mean "not created yet". We
        only trust it once a few spaced-out ids above it are all missing too.
        """
        return not (
            self._id_exists(ad_id)
            or self._id_exists(ad_id + 5)
            or self._id_exists(ad_id + 13)
        )

    def current_max_id(self, seed: Optional[int] = None) -> int:
        """Return the id of the newest ad that currently exists, site-wide.

        Ad ids are handed out in increasing order across all of Kleinanzeigen, so
        the largest live id is the newest ad. We find it by walking up from a
        known-live id (the newest search result, or ``seed``) until ids stop
        existing, then binary-searching the boundary. Deleted-ad gaps are handled
        by _beyond_max.
        """
        if seed is None:
            _, newest = self.search_page(size=1, sort_type="DATE_DESCENDING")
            if not newest:
                raise RuntimeError(
                    "search returned no ads, so there's no id to start from — "
                    "pass a seed (any recent ad id) instead"
                )
            seed = int(newest[0].id)
        lo = int(seed)
        # exponential search upward for a point that is past the frontier
        step, hi = 64, lo
        while not self._beyond_max(hi + step):
            hi += step
            step *= 2
            if step > 4_000_000:  # ~3 days of ads; safety valve
                break
        a, b = hi, hi + step  # a exists, b is beyond the frontier
        while b - a > 1:
            m = (a + b) // 2
            if self._id_exists(m) or not self._beyond_max(m):
                a = m
            else:
                b = m
        return a

    def _ad_matches(
        self,
        listing: Listing,
        *,
        category_id,
        q_terms,
        exclude_terms,
        min_price,
        max_price,
        price_type,
        poster_type,
        near,
        radius_km,
        match,
    ) -> bool:
        """Client-side filter for iter_new_ads (the by-id endpoint can't filter)."""
        if category_id is not None and listing.category_id != category_id:
            return False
        if price_type is not None:
            lpt = (listing.price_type or "").upper()
            free = {"FREE", "GIVE_AWAY"}
            if price_type in free:
                if lpt not in free:
                    return False
            elif lpt != price_type:
                return False
        if min_price is not None and (
            listing.price is None or listing.price < min_price
        ):
            return False
        if max_price is not None and (
            listing.price is None or listing.price > max_price
        ):
            return False
        if (
            poster_type is not None
            and (listing.poster_type or "").upper() != poster_type
        ):
            return False
        if q_terms:
            hay = f"{listing.title}\n{listing.description}".lower()
            if not all(t in hay for t in q_terms):
                return False
        if _excluded(listing, exclude_terms):
            return False
        if near is not None and radius_km is not None:
            if listing.latitude is None or listing.longitude is None:
                return False
            if (
                _haversine_km(near[0], near[1], listing.latitude, listing.longitude)
                > radius_km
            ):
                return False
        if match is not None and not match(listing):
            return False
        return True

    def iter_new_ads(
        self,
        *,
        mode: str = "search",
        location=None,
        distance_km=None,
        start_id: Optional[int] = None,
        backfill: bool = False,
        category_id=None,
        q=None,
        exclude=None,
        min_price=None,
        max_price=None,
        price_type=None,
        poster_type=None,
        near=None,
        radius_km=None,
        match=None,
        poll_interval: float = 15.0,
        max_per_poll: int = 1200,
        retry_missing_for: float = 300.0,
    ):
        """Yield newly-posted ads as they appear, as an endless generator.

        Two modes (see the README for the full story):
          - "search" (default): polls newest-first search, dodging the site's
            ~2-minute result cache by changing the page size every request.
            New ads arrive ~15-45s after going live, a few requests/minute.
          - "frontier": fetches every new ad by id the moment it exists.
            Seconds-fast, but needs ~15-20 requests/second to keep up — short,
            narrow watches only.

        Filters are ANDed: category_id, q, exclude, min_price, max_price,
        price_type ("FREE" = zu verschenken), poster_type, near=(lat, lon) +
        radius_km, and match=callable(Listing) -> bool. location/distance_km
        only work in search mode; use near/radius_km in frontier mode.

        Only ads posted after you start watching are yielded; backfill=True
        also emits the current batch first. Frontier-mode extras: start_id
        resumes from a known id, max_per_poll caps one pass, and ids that 404
        are re-checked for retry_missing_for seconds (some ads are held back
        briefly before going public).
        """
        if mode not in ("search", "frontier"):
            raise ValueError('mode must be "search" or "frontier"')
        if mode == "search" and start_id is not None:
            raise ValueError("start_id only makes sense in frontier mode")
        if mode == "frontier" and (location is not None or distance_km is not None):
            raise ValueError(
                "location/distance_km only work in search mode — "
                "frontier mode filters by near=(lat, lon) + radius_km"
            )

        pt = price_type.upper() if price_type else None
        poster = poster_type.upper() if poster_type else None
        exclude_terms = _as_terms(exclude)

        if mode == "search":
            return self._iter_new_ads_search(
                location=location,
                distance_km=distance_km,
                category_id=category_id,
                q=q,
                exclude_terms=exclude_terms,
                min_price=min_price,
                max_price=max_price,
                price_type=pt,
                poster_type=poster,
                near=near,
                radius_km=radius_km,
                match=match,
                backfill=backfill,
                poll_interval=poll_interval,
            )
        return self._iter_new_ads_frontier(
            start_id=start_id,
            backfill=backfill,
            category_id=category_id,
            q=q,
            exclude_terms=exclude_terms,
            min_price=min_price,
            max_price=max_price,
            price_type=pt,
            poster_type=poster,
            near=near,
            radius_km=radius_km,
            match=match,
            poll_interval=poll_interval,
            max_per_poll=max_per_poll,
            retry_missing_for=retry_missing_for,
        )

    def _iter_new_ads_search(
        self,
        *,
        location,
        distance_km,
        category_id,
        q,
        exclude_terms,
        min_price,
        max_price,
        price_type,
        poster_type,
        near,
        radius_km,
        match,
        backfill,
        poll_interval,
    ):
        """The cheap watcher: poll newest-first search, dodge their cache."""
        location_id = self._location_to_id(location) if location else None
        # their cache is per exact query, so a different page size each time
        # means we never get a cached (stale) answer
        sizes = list(range(25, 101))
        random.shuffle(sizes)
        seen: dict = {}  # ids we already yielded (or saw on the first page)
        watermark = None  # only ads posted after this count as new
        first = True
        n = 0
        while True:
            _, listings = self.search_page(
                category_id=category_id,
                location_id=location_id,
                distance_km=distance_km,
                min_price=min_price,
                max_price=max_price,
                q=q,
                sort_type="DATE_DESCENDING",
                size=sizes[n % len(sizes)],
            )
            n += 1
            if first:
                # later polls use bigger pages that reach further into the
                # past; the cut-off stops old ads showing up as "new"
                stamps = [
                    d for listing in listings if (d := _posted_dt(listing.posted))
                ]
                if stamps:
                    watermark = min(stamps) if backfill else max(stamps)
            for listing in reversed(listings):  # oldest first, like a live feed
                if listing.id in seen:
                    continue
                seen[listing.id] = None
                if len(seen) > 5000:  # keep the seen-set from growing forever
                    seen.pop(next(iter(seen)))
                if first and not backfill:
                    continue  # first page is just our starting point
                posted = _posted_dt(listing.posted)
                if watermark and posted and posted < watermark:
                    continue  # older than where we started watching
                # category/price/q were already filtered by the server here
                if self._ad_matches(
                    listing,
                    category_id=None,
                    q_terms=[],
                    exclude_terms=exclude_terms,
                    min_price=None,
                    max_price=None,
                    price_type=price_type,
                    poster_type=poster_type,
                    near=near,
                    radius_km=radius_km,
                    match=match,
                ):
                    yield listing
            first = False
            time.sleep(max(0.0, poll_interval))

    def _iter_new_ads_frontier(
        self,
        *,
        start_id,
        backfill,
        category_id,
        q,
        exclude_terms,
        min_price,
        max_price,
        price_type,
        poster_type,
        near,
        radius_km,
        match,
        poll_interval,
        max_per_poll,
        retry_missing_for,
    ):
        """The fast watcher: walk the ad ids upward and fetch each new one."""
        cat = str(category_id) if category_id is not None else None
        q_terms = [t for t in (q or "").lower().split() if t]

        def matches(listing):
            return self._ad_matches(
                listing,
                category_id=cat,
                q_terms=q_terms,
                exclude_terms=exclude_terms,
                min_price=min_price,
                max_price=max_price,
                price_type=price_type,
                poster_type=poster_type,
                near=near,
                radius_km=radius_km,
                match=match,
            )

        if start_id is not None:
            last = int(start_id)
        elif backfill:
            _, newest = self.search_page(size=1, sort_type="DATE_DESCENDING")
            last = (int(newest[0].id) - 1) if newest else self.current_max_id()
        else:
            last = self.current_max_id()

        pending: dict = {}  # id -> when we first got a 404 for it
        last_warned = 0.0
        while True:
            # give recent 404s another look — ads held back by moderation
            # would be lost for good if we only tried each id once
            for ad_id in sorted(pending):
                if time.time() - pending[ad_id] > retry_missing_for:
                    del pending[ad_id]
                    continue
                listing = self.try_get_ad(ad_id)
                if listing is not None:
                    del pending[ad_id]
                    if matches(listing):
                        yield listing

            # walk forward from where we stopped last time
            fetched = 0
            at_frontier = False
            while fetched < max_per_poll:
                listing = self.try_get_ad(last + 1)
                fetched += 1
                if listing is None:
                    # a 404 is either a gap (deleted / still being checked)
                    # or the end of the ids so far — _beyond_max tells which
                    if self._beyond_max(last + 1):
                        at_frontier = True
                        break
                    pending[last + 1] = time.time()
                    last += 1
                    continue
                last += 1
                if matches(listing):
                    yield listing

            if at_frontier:
                time.sleep(max(0.0, poll_interval))
            elif fetched >= max_per_poll and time.time() - last_warned > 60:
                print(
                    "iter_new_ads: falling behind (new ads come in faster "
                    "than we fetch) — lower rate_limit or switch to the "
                    "default search mode",
                    file=sys.stderr,
                )
                last_warned = time.time()

    def watch_new_ads(self, callback, **kwargs) -> None:
        """Call ``callback(Listing)`` for every new matching ad, forever.

        Convenience wrapper around iter_new_ads; takes the same keyword filters.
        """
        for listing in self.iter_new_ads(**kwargs):
            callback(listing)

    # -- categories (offline, bundled catalog) ------------------------------ #
    @staticmethod
    def categories() -> list:
        """Return the bundled category catalog as a list of Category objects."""
        return _catalog.all_categories()

    @staticmethod
    def find_categories(query: str, limit: int = 8) -> list:
        """Search the bundled catalog by name/path, best match first."""
        return _catalog.find_categories(query, limit=limit)

    @staticmethod
    def get_category(category_id):
        """Return the Category for this id, or None if it's not found."""
        return _catalog.get_category(category_id)

    @staticmethod
    def resolve_category(value):
        """Convert a category name or id to an id string (None means all categories)."""
        return _catalog.resolve_category(value)

    def fetch_categories(self) -> list:
        """Download the live category list and return it as Category objects.

        Use this to rebuild the bundled data/categories.json when the site
        changes its categories.
        """
        data = self._get(f"{API_HOST}/api/categories.json").json()
        return [_catalog.Category(**c) for c in _catalog.flatten_api_categories(data)]

    # -- logged-in: who am I ----------------------------------------------- #
    @property
    def user_id(self) -> str:
        """numeric account id. The chat and own-ad URLs need it.

        We look it up once from your email and remember it. You can also pass
        user_id= to the constructor to skip this lookup.
        """
        if self._user_id:
            return self._user_id
        auth = self._require_auth()
        email = auth.email
        if not email:
            raise RuntimeError(
                "Could not work out the account id: no email in the login. "
                "Pass user_id=... to KleinanzeigenAPI instead."
            )
        # this needs a logged-in request, so use _request with authed=True
        r = self._request(
            "GET", f"{API_HOST}/api/users/{email}/profile.json", authed=True
        )
        body = r.json()
        uid = (body.get("data") or {}).get("id") or body.get("id")
        if not uid:
            raise RuntimeError(f"Unexpected profile.json shape: {str(body)[:200]}")
        self._user_id = str(uid)
        return self._user_id

    # -- logged-in: chat --------------------------------------------------- #
    def conversations(self, page: int = 0, size: int = 100) -> list:
        """List your chat threads, newest first. Returns Conversation objects."""
        uid = self.user_id
        r = self._request(
            "GET",
            f"{GATEWAY_HOST}/messagebox/api/users/{uid}/conversations",
            params={"page": page, "size": size},
            authed=True,
            gateway=True,
        )
        body = r.json()
        items = body.get("conversations") or body.get("data") or []
        if isinstance(items, dict):
            items = items.get("conversations") or items.get("items") or []
        return [_parse_conversation(c) for c in items if isinstance(c, dict)]

    def conversation(self, conversation_id: str) -> dict:
        """Open one thread and return the full chat object with its messages.

        This is a PUT, not a GET - that's just how the API works here (opening a
        thread also marks it as loaded). The messages are under the "messages"
        key. See messages() for a simpler view.
        """
        uid = self.user_id
        r = self._request(
            "PUT",
            f"{GATEWAY_HOST}/messagebox/api/users/{uid}/conversations/{conversation_id}",
            params={"contentWarnings": "true"},
            authed=True,
            gateway=True,
        )
        return r.json()

    def messages(self, conversation_id: str) -> list:
        """Return a thread's messages as simple dicts, oldest first.

        Each one looks like {"text", "direction", "date", "raw"}, where
        direction is "sent" or "received".
        """
        conv = self.conversation(conversation_id)
        raw = conv.get("messages") or (conv.get("data") or {}).get("messages") or []
        out = []
        for m in raw if isinstance(raw, list) else []:
            if not isinstance(m, dict):
                continue
            bound = (m.get("boundness") or m.get("direction") or "").upper()
            direction = (
                "received"
                if "IN" in bound
                else "sent"
                if "OUT" in bound
                else bound.lower()
            )
            out.append(
                {
                    "text": _message_text(m),
                    "direction": direction,
                    "date": m.get("receivedDate") or "",
                    "raw": m,
                }
            )
        return out

    def reply(self, conversation_id: str, text: str) -> None:
        """Send a text reply in an existing conversation."""
        uid = self.user_id
        self._request(
            "POST",
            f"{GATEWAY_HOST}/messagebox/api/users/{uid}/conversations/{conversation_id}",
            params={
                "warnPhoneNumber": "false",
                "warnEmail": "false",
                "warnBankDetails": "false",
            },
            json={"message": text},
            authed=True,
            gateway=True,
        )

    def mark_read(self, conversation_ids) -> None:
        """Mark one or more conversations as read."""
        uid = self.user_id
        if isinstance(conversation_ids, str):
            conversation_ids = [conversation_ids]
        ids = ",".join(str(c) for c in conversation_ids)
        self._request(
            "POST",
            f"{GATEWAY_HOST}/messagebox/api/users/{uid}/conversations/read",
            params={"ids": ids},
            authed=True,
            gateway=True,
        )

    def start_conversation(self, ad_id: str, contact_name: str) -> dict:
        """Start a new chat on an ad (the first message to a seller).

        Returns the new conversation. Use its id with reply() to keep chatting.
        """
        uid = self.user_id
        r = self._request(
            "POST",
            f"{API_HOST}/api/users/{uid}/create-conversation/{ad_id}",
            params={"contactName": contact_name},
            authed=True,
        )
        return r.json()

    # -- logged-in: your own ads ------------------------------------------- #
    def my_ads(
        self,
        page: int = 0,
        size: int = 25,
        sort_type: Optional[str] = None,
        q: Optional[str] = None,
    ) -> list:
        """List your own ads (both live and paused) as Listing objects.

        Pass q to filter your ads by a keyword.
        """
        uid = self.user_id
        params = {"_in": ADS_FIELD_SELECTOR, "page": page, "size": size}
        if sort_type:
            params["sortType"] = sort_type
        if q:
            params["q"] = q
        data = self._request(
            "GET", f"{API_HOST}/api/users/{uid}/ads.json", params=params, authed=True
        ).json()
        return self._parse_ads_block(data)[1]

    def get_my_ad(self, ad_id: str) -> Listing:
        """Fetch one of your own ads (works for paused ads too)."""
        uid = self.user_id
        data = self._request(
            "GET", f"{API_HOST}/api/users/{uid}/ads/{ad_id}.json", authed=True
        ).json()
        ad = data.get("{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad", data)
        ad = ad.get("value", ad) if isinstance(ad, dict) else ad
        return self._parse_ad(ad)

    def pause_ad(self, ad_id: str) -> None:
        """Take one of your ads offline (reversible with activate_ad)."""
        uid = self.user_id
        self._request(
            "PUT", f"{API_HOST}/api/users/{uid}/ads/paused/{ad_id}.json", authed=True
        )

    def activate_ad(self, ad_id: str) -> None:
        """Bring a paused ad back online."""
        uid = self.user_id
        self._request(
            "PUT", f"{API_HOST}/api/users/{uid}/ads/active/{ad_id}.json", authed=True
        )

    def delete_ad(self, ad_id: str) -> None:
        """Permanently delete one of your ads."""
        uid = self.user_id
        self._request("DELETE", f"{API_HOST}/api/users/{uid}/ads/{ad_id}", authed=True)

    def extend_ad(self, ad_id: str) -> None:
        """Renew/extend one of your ads (bumps its expiry)."""
        uid = self.user_id
        self._request(
            "POST", f"{API_HOST}/api/users/{uid}/ads/extend/{ad_id}", authed=True
        )

    def extend_status(self, ad_ids) -> list:
        """Return the renew/extend eligibility for one or more of your ads."""
        uid = self.user_id
        if isinstance(ad_ids, str):
            ad_ids = [ad_ids]
        ids = ",".join(str(a) for a in ad_ids)
        return self._request(
            "GET",
            f"{API_HOST}/api/users/{uid}/ads/extend/status",
            params={"adids": ids},
            authed=True,
        ).json()

    def watchlist(self, page: int = 0, size: int = 25) -> list:
        """List the ads you've saved to your watchlist. Returns a list of Listing."""
        uid = self.user_id
        data = self._request(
            "GET",
            f"{API_HOST}/api/users/{uid}/watchlist.json",
            params={"_in": ADS_FIELD_SELECTOR, "page": page, "size": size},
            authed=True,
        ).json()
        return self._parse_ads_block(data)[1]

    # -- logged-in: post a new ad ------------------------------------------ #
    def post_ad(
        self,
        *,
        title: str,
        description: str,
        category_id,
        location_id,
        price=None,
        price_type: str = "FIXED",
        poster_type: str = "PRIVATE",
        ad_type: str = "OFFERED",
        contact_name: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        attributes: Optional[dict] = None,
        picture_urls: Optional[list] = None,
        latitude=None,
        longitude=None,
    ) -> str:
        """Post a new ad and return its id.

        A few things the API is picky about:
          - location_id has to be a real place (a city/postcode like "13467
            Reinickendorf"), not a whole region like "Berlin", or it's rejected.
          - email is required. We default it to your account email; without any
            email the API fails with a 500.

        price_type is "FIXED" (needs a price), "NEGOTIABLE" (price is what you're
        asking), or "FREE" (zu verschenken, no price). attributes is a
        {name: value} dict of category-specific fields. picture_urls is a list of
        already-uploaded image URLs.
        """
        uid = self.user_id
        if email is None:
            email = getattr(self._auth_provider, "email", None)
        xml = self._build_ad_xml(
            title=title,
            description=description,
            category_id=category_id,
            location_id=location_id,
            price=price,
            price_type=price_type,
            poster_type=poster_type,
            ad_type=ad_type,
            contact_name=contact_name or "",
            email=email or "",
            phone=phone,
            attributes=attributes or {},
            picture_urls=picture_urls or [],
            latitude=latitude,
            longitude=longitude,
        )
        r = self._request(
            "POST",
            f"{API_HOST}/api/users/{uid}/ads.json",
            data=xml,
            content_type="application/xml",
            authed=True,
        )
        # the new id comes back either in a Location header or in the ad body
        loc = r.headers.get("Location") or r.headers.get("location") or ""
        if loc:
            tail = loc.rstrip("/").split("/")[-1].split(".")[0]
            if tail.isdigit():
                return tail
        try:
            body = r.json()
            ad = body.get("{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad", body)
            ad = ad.get("value", ad) if isinstance(ad, dict) else ad
            return str(_val(ad.get("id")) or "")
        except Exception:
            return ""

    # XML namespaces the ad body has to declare.
    _AD_NAMESPACES = {
        "types": "types/v1",
        "cat": "category/v1",
        "ad": "ad/v1",
        "loc": "location/v1",
        "attr": "attribute/v1",
        "pic": "picture/v1",
        "user": "user/v1",
        "rate": "rate/v1",
        "reply": "reply/v1",
        "feed": "feed/v1",
        "shipping": "shipping/v1",
        "document": "document/v1",
        "payment": "payment/v1",
        "medias": "media/v1",
        "ps": "productsafety/v1",
    }
    # our easy price-type names -> the value the API actually wants.
    _PRICE_TYPE_MAP = {
        "FIXED": "SPECIFIED_AMOUNT",
        "SPECIFIED_AMOUNT": "SPECIFIED_AMOUNT",
        "NEGOTIABLE": "PLEASE_CONTACT",
        "VB": "PLEASE_CONTACT",
        "PLEASE_CONTACT": "PLEASE_CONTACT",
        "FREE": "FREE",
        "GIVE_AWAY": "FREE",
    }

    @classmethod
    def _build_ad_xml(
        cls,
        *,
        title,
        description,
        category_id,
        location_id,
        price,
        price_type,
        poster_type,
        ad_type,
        contact_name,
        email,
        phone,
        attributes,
        picture_urls,
        latitude,
        longitude,
    ) -> str:
        """Build the XML body for a new ad. The API takes XML here, not JSON."""
        from xml.sax.saxutils import escape, quoteattr

        def esc(v):
            # escape text so a stray < or & in a title can't break the XML
            return escape("" if v is None else str(v))

        ns = " ".join(
            f'xmlns:{p}="http://www.ebayclassifiedsgroup.com/schema/{s}"'
            for p, s in cls._AD_NAMESPACES.items()
        )
        pt = cls._PRICE_TYPE_MAP.get(str(price_type).upper(), "SPECIFIED_AMOUNT")

        parts = [
            "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>",
            f'<ad:ad {ns} locale="en_US" id="0">',
            f"<ad:title>{esc(title)}</ad:title>",
            f"<ad:description>{esc(description)}</ad:description>",
        ]
        if contact_name:
            parts.append(f"<ad:contact-name>{esc(contact_name)}</ad:contact-name>")
        if email:  # the server returns 500 without a contact email
            parts.append(f"<ad:email>{esc(email)}</ad:email>")
        if phone:
            parts.append(f"<ad:phone>{esc(phone)}</ad:phone>")
        parts.append(
            f"<ad:poster-type><ad:value>{esc(poster_type)}</ad:value></ad:poster-type>"
        )
        parts.append(f"<ad:ad-type><ad:value>{esc(ad_type)}</ad:value></ad:ad-type>")
        parts.append(f"<cat:category id={quoteattr(str(category_id))}/>")
        parts.append(
            f"<loc:locations><loc:location id={quoteattr(str(location_id))}/>"
            f"</loc:locations>"
        )
        addr = []
        if latitude is not None:
            addr.append(f"<types:latitude>{esc(latitude)}</types:latitude>")
        if longitude is not None:
            addr.append(f"<types:longitude>{esc(longitude)}</types:longitude>")
        addr.append("<types:show-full-address>false</types:show-full-address>")
        parts.append("<ad:ad-address>" + "".join(addr) + "</ad:ad-address>")
        price_parts = [
            f"<types:price-type><types:value>{pt}</types:value>" "</types:price-type>"
        ]
        if pt != "FREE" and price is not None:
            price_parts.append(f"<types:amount>{esc(price)}</types:amount>")
        parts.append("<ad:price>" + "".join(price_parts) + "</ad:price>")
        pics = "".join(
            f'<pic:picture><pic:link rel="XXL" href={quoteattr(str(u))}/>'
            f"</pic:picture>"
            for u in picture_urls
        )
        parts.append(f"<pic:pictures>{pics}</pic:pictures>")
        attr_xml = "".join(
            f"<attr:attribute name={quoteattr(str(k))}>"
            f"<attr:value>{esc(v)}</attr:value></attr:attribute>"
            for k, v in attributes.items()
        )
        parts.append(f"<attr:attributes>{attr_xml}</attr:attributes>")
        if str(ad_type).upper() == "OFFERED":
            parts.append('<payment:buy-now selected="false"/>')
        parts.append("</ad:ad>")
        return "".join(parts)
