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
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from curl_cffi import requests as creq

from . import categories as _catalog

API_HOST = "https://api.kleinanzeigen.de"
WEB_HOST = "https://www.kleinanzeigen.de"
ADS_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ads"
LOCATIONS_NS = "{http://www.ebayclassifiedsgroup.com/schema/location/v1}locations"
SEARCH_META_NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ads-search-options"

# --- baked-in app-distribution values (override via env / constructor) -------
APP_VERSION = "2026.23.1"
DEFAULT_BASIC_USER = "android"
DEFAULT_BASIC_PW = "TaR60pEttY"

CATEGORY_WOHNUNG_MIETEN = 203


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
    images: list = field(default_factory=list)
    attributes: dict = field(default_factory=dict)  # localized-label -> value

    def to_dict(self) -> dict:
        return asdict(self)


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
    """

    def __init__(self, rate_limit: float = 1.5, app_version: str = APP_VERSION,
                 timeout: int = 25, max_retries: int = 3,
                 basic_user: Optional[str] = None, basic_pw: Optional[str] = None):
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

    # -- transport ---------------------------------------------------------- #
    def _headers(self) -> dict:
        return {
            "X-EBAYK-APP": self._xapp,
            "X-ECG-USER-AGENT": f"ebayk-android-app-{self.app_version}",
            "X-ECG-USER-VERSION": self.app_version,
            "User-Agent": f"Kleinanzeigen/{self.app_version} (Android 13; Pixel 7)",
            "Accept": "application/json",
            "Accept-Language": "de-DE",
            "Authorization": self._auth,
        }

    def _throttle(self):
        wait = self.rate_limit - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))

    def _get(self, url: str, params: Optional[dict] = None) -> creq.Response:
        last = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                r = self._s.get(url, params=params, headers=self._headers(), timeout=self.timeout)
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
        r = self._s.get(f"{WEB_HOST}/s-ort-empfehlungen.json",
                        params={"query": query},
                        headers={"X-Requested-With": "XMLHttpRequest",
                                 "Accept-Language": "de-DE"},
                        timeout=self.timeout)
        out = []
        for k, label in r.json().items():
            lid = k.lstrip("_")
            if lid != "0":
                out.append((lid, label))
        return out

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
            if name.endswith(".qm"):            # wohnung_mieten.qm / haus_mieten.qm
                size = _num(v)
            elif name.endswith(".zimmer"):      # *_mieten.zimmer
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
            images=images,
            attributes=attrs,
        )

    # -- search ------------------------------------------------------------- #
    def search_page(self, *, category_id=None, location_id=None,
                    distance_km=None, min_price=None, max_price=None,
                    ad_type="OFFERED", q=None, picture_required=False,
                    sort_type=None, page=0, size=25) -> tuple:
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
        if sort_type:  # PRICE_ASCENDING | PRICE_DESCENDING | DATE_DESCENDING | DISTANCE_ASCENDING
            params["sortType"] = sort_type

        data = self._get(f"{API_HOST}/api/ads.json", params=params).json()
        block = data.get(ADS_NS, {}).get("value", {})
        total = int(_num(block.get("paging", {}).get("numFound")) or 0)
        raw = block.get("ad", [])
        if isinstance(raw, dict):  # capi returns a single object when 1 result
            raw = [raw]
        return total, [self._parse_ad(a) for a in raw]

    def search(self, location=None, *, q=None, exclude=None, category=None,
               category_id=None, distance_km=None, min_price=None, max_price=None,
               min_rooms=None, max_rooms=None, min_size=None, max_size=None,
               ad_type="OFFERED", sort_type=None, pages=1, size=25,
               sort_by_price=False) -> list:
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
        category_id = _catalog.resolve_category(category if category is not None else category_id)
        if sort_by_price and not sort_type:  # default to cheapest-first
            sort_type = "PRICE_ASCENDING"
        exclude_terms = _as_terms(exclude)
        location_id = None
        if location:
            if str(location).isdigit():
                location_id = str(location)
            else:
                best = self.best_location(location)
                if not best:
                    raise ValueError(
                        f"Could not resolve location {location!r}. Check the spelling "
                        f"with resolve_location(), pass a numeric location id, or use "
                        f"location=None to search all of Germany."
                    )
                location_id = best[0]

        results, seen = [], set()
        for page in range(pages):
            total, listings = self.search_page(
                category_id=category_id, location_id=location_id, distance_km=distance_km,
                min_price=min_price, max_price=max_price, ad_type=ad_type, q=q,
                sort_type=sort_type, page=page, size=size)
            if not listings:
                break
            for l in listings:
                if l.id in seen:
                    continue
                if _excluded(l, exclude_terms):
                    continue
                if min_rooms is not None and (l.rooms is None or l.rooms < min_rooms):
                    continue
                if max_rooms is not None and (l.rooms is None or l.rooms > max_rooms):
                    continue
                if min_size is not None and (l.size_m2 is None or l.size_m2 < min_size):
                    continue
                if max_size is not None and (l.size_m2 is None or l.size_m2 > max_size):
                    continue
                seen.add(l.id)
                results.append(l)
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
        cat = _catalog.resolve_category(category if category is not None else category_id)
        if cat is None:
            raise ValueError("search_metadata needs a category (name or id)")
        data = self._get(f"{API_HOST}/api/ads/search-metadata/{cat}.json").json()
        opts = _val(data.get(SEARCH_META_NS, {}))
        out: dict = {}
        for name, spec in (opts.items() if isinstance(opts, dict) else []):
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
                entry["values"] = [(v.get("value"), v.get("localized-label"))
                                   for v in sv if isinstance(v, dict)]
            out[name] = entry
        return out

    def get_ad(self, ad_id: str) -> Listing:
        """Fetch a single ad by id."""
        data = self._get(f"{API_HOST}/api/ads/{ad_id}.json").json()
        # single-ad payload wraps under an "ad" key
        ad = data.get("{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad", data)
        ad = ad.get("value", ad) if isinstance(ad, dict) else ad
        return self._parse_ad(ad)

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
