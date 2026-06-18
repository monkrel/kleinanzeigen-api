# kleinanzeigen-api

Unofficial Python client + CLI for **[kleinanzeigen.de](https://www.kleinanzeigen.de)**,
Germany's classifieds marketplace (formerly *eBay Kleinanzeigen*). It talks to
the same mobile JSON API (`api.kleinanzeigen.de`) the official Android app uses.

- **Search any category** — cars, electronics, furniture, jobs, rentals… — by
  keyword, location, price, and more (not just apartments).
- **`exclude` terms** to drop unwanted results (e.g. `defekt`, `bastler`).
- Returns **structured data the website never exposes**: GPS coordinates, exact
  result counts, typed attributes (Wohnfläche, Zimmer, Nebenkosten, …), all
  image sizes, ISO timestamps and price type.
- **Log in (optional)** to read and answer your chats, manage your own ads
  (pause / activate / delete / renew), and even post new ones.


> If this saved you some time, please **★ star the repo** — it's the main signal
> that tells me it's worth maintaining. (Lots of people clone it, almost nobody
> stars it, and it's a little lonely up there. :( ))

> [!NOTE]
> This is for **Germany's kleinanzeigen.de only**, and is **not affiliated with, authorized, or endorsed
> by** Kleinanzeigen GmbH / Adevinta. It talks to a private app API and is
> provided for educational and personal use. See [Legal & etiquette](#legal--etiquette).

## Install

```bash
pip install kleinanzeigen-api
```

## Quickstart (library)

```python
from kleinanzeigen_api import KleinanzeigenAPI

api = KleinanzeigenAPI()

# Search EVERY category by keyword, newest first, excluding junk
ads = api.search(q="ThinkPad X1", exclude=["defekt", "bastler"],
                 sort_type="DATE_DESCENDING", pages=2)
for a in ads:
    print(a.price, a.zip_code, a.city, a.title, a.url)

# Restrict to a category by NAME (or id) + a location, cheapest first
bikes = api.search("Berlin", category="Fahrräder & Zubehör", q="Rennrad",
                   distance_km=20, max_price=400, sort_type="PRICE_ASCENDING")

# Convenience wrapper for apartment rentals (category 203 = Mietwohnungen)
flats = api.search_rentals("Oranienburg", max_price=900, min_rooms=2,
                           exclude=["tausch", "wg-zimmer"],  # works here too
                           sort_type="PRICE_ASCENDING")
for f in flats:
    print(f.price, f.rooms, f.size_m2, f.latitude, f.longitude, f.attributes)

# A single ad by id
ad = api.get_ad("123456789")
```

`exclude` takes a string or a list and drops any result whose **title or
description** contains one of those terms (case-insensitive, applied
client-side). `q` is the server-side keyword.

### `Listing` fields

```
id, title, description, price, price_type, url, city, zip_code,
latitude, longitude, size_m2, rooms, posted, poster_type, images, attributes
```

`attributes` is a `{localized_label: value}` dict of everything the ad carries
(`size_m2` and `rooms` are also surfaced as typed top-level fields).

## Quickstart (CLI)

Installing the package also adds a `kleinanzeigen-api` command
(`python -m kleinanzeigen_api` works too):

```bash
# Search ALL categories by keyword, newest first, excluding junk
kleinanzeigen-api --q "ThinkPad X1" --exclude defekt,bastler --sort new

# Berlin within 20 km, a keyword, cheapest first, JSON to a file
kleinanzeigen-api Berlin --distance 20 --q "Rennrad" --max-price 400 \
    --sort cheap --json --out bikes.json

# Restrict to a category id (203 = Wohnung mieten), 2+ rooms, 3 pages
kleinanzeigen-api Oranienburg --category 203 --max-price 900 \
    --min-rooms 2 --pages 3 --sort cheap
```

`--exclude` is repeatable or comma-separated. You can pass a numeric location id
instead of a name (`kleinanzeigen-api 3331` == Berlin). Run
`kleinanzeigen-api --help` for all flags.

## Logged-in features (optional) — chat, your ads, posting

Everything above works without an account. If you log in, you can also read and
answer your **chats**, manage your **own ads**, see your **watchlist**, and
**post** a new ad. It's the same login the app uses, so it's a one-time sign-in.

> [!WARNING]
> Automating a logged-in account is against Kleinanzeigen's Terms of Service and
> can get the account banned. Keep this personal and low-volume.

### Log in once

```bash
kleinanzeigen-api login
```

It prints a link — sign in, then paste back the URL the browser lands on (it has
a `?code=` in it). The token is saved to `~/.kleinanzeigen_api/token.json` and
refreshed automatically, so you only do this once.

**No browser on the machine (server / CI)?** You don't need one there:

- log in once on any machine that has a browser, then copy `token.json` over (or
  point `KLEINANZEIGEN_TOKEN_DIR` at it), or
- set `KLEINANZEIGEN_REFRESH_TOKEN` to a refresh token you already have (useful
  when it comes from a CI secret).

### From the library

```python
from kleinanzeigen_api import KleinanzeigenAPI, Authenticator

auth = Authenticator()
if not auth.logged_in:
    auth.login_interactive()              # one-time browser sign-in

api = KleinanzeigenAPI(authenticator=auth)

# --- chat ---
for c in api.conversations():
    print(c.counterparty, "-", c.ad_title)

for m in api.messages("<conversation_id>"):
    print(m["direction"], m["text"])       # "sent" / "received"

api.reply("<conversation_id>", "Hallo, ist das noch verfügbar?")

# --- your own ads ---
mine = api.my_ads()
api.pause_ad("<ad_id>")
api.activate_ad("<ad_id>")
api.delete_ad("<ad_id>")

saved = api.watchlist()

# --- post a new ad (returns the new id) ---
new_id = api.post_ad(
    title="Sofa zu verschenken",
    description="Gut erhalten, Abholung in Berlin.",
    category_id=192,                       # "Verschenken"
    location_id="3455",                    # 13467 Reinickendorf
    price_type="FREE",                     # or "FIXED" with price=..., or "NEGOTIABLE"
)
```

### From the CLI

```bash
kleinanzeigen-api login
kleinanzeigen-api chats
kleinanzeigen-api messages <conversation_id>
kleinanzeigen-api reply <conversation_id> "Hallo, ist das noch da?"

kleinanzeigen-api my-ads
kleinanzeigen-api watchlist
kleinanzeigen-api pause <ad_id>
kleinanzeigen-api activate <ad_id>
kleinanzeigen-api delete <ad_id>
kleinanzeigen-api extend <ad_id>

kleinanzeigen-api post --title "Sofa zu verschenken" \
    --description "Gut erhalten, Abholung in Berlin." \
    --category 192 --location "13467 Reinickendorf" --price-type FREE
```

Two things to know when posting:

- the **location has to be a real place** (a city/postcode like
  `13467 Reinickendorf`), not a broad region like `Berlin`, or the API rejects it;
- a **contact email is required** — by default we use your account email.

## Categories — you never need to memorize ids

Pass a **name or an id** to `category`; names are resolved against a catalog of
all ~159 categories **bundled with the package** (works offline). Unknown or
ambiguous names raise a `ValueError` listing concrete suggestions.

```python
from kleinanzeigen_api import find_categories, KleinanzeigenAPI

find_categories("Fahrr")        # -> [Category(id='217', name='Fahrräder & Zubehör', …)]
KleinanzeigenAPI().search(category="Notebooks", q="ThinkPad")   # by name
KleinanzeigenAPI().search(category=161, q="ThinkPad")           # or by id
```

From the CLI, browse with `--categories` (offline, no request):

```bash
kleinanzeigen-api --categories            # list all
kleinanzeigen-api --categories fahrr      # filter
#   217  Auto, Rad & Boot > Fahrräder & Zubehör
```

> Category **names are German** (it's a German marketplace) — e.g. `Notebooks`,
> `Fahrräder & Zubehör`, `Mietwohnungen`. When unsure, `find_categories(...)` or
> `--categories <query>` shows the exact name and id. The bundled catalog can be
> refreshed from the live API with `KleinanzeigenAPI().fetch_categories()`.

## Search parameters

`search(location=None, *, q, exclude, category, category_id, distance_km,
min_price, max_price, min_rooms, max_rooms, min_size, max_size, ad_type,
sort_type, pages, size)`

- **location** — city/region name (resolved automatically via the app's
  `/api/locations.json` endpoint, with the website autocomplete as a fallback)
  or a numeric id. An unresolvable name raises `ValueError` (no silent
  nationwide fallback); pass `location=None` to deliberately search all of
  Germany.
- **category** — name **or** id; `None` (default) searches **all categories**.
  `category_id` is the raw-id alias (pass only one).
- **q** — server-side keyword. **exclude** — string or list; drops results whose
  title/description contains any term (client-side, case-insensitive).
- **sort_type** — `PRICE_ASCENDING`, `PRICE_DESCENDING`, `DATE_DESCENDING`,
  `DISTANCE_ASCENDING` (server-side).
- **min_rooms / max_rooms / min_size / max_size** — applied client-side; ignored
  for ads without those attributes.
- **ad_type** — `OFFERED` (default) or `WANTED`. **pages / size** — paging.

`search_rentals(location, **kwargs)` is the same thing with `category_id=203`
("Mietwohnungen", apartments to rent) pre-set.

## Other helpers

- `resolve_location(query)` / `best_location(query)` — turn a place name into
  `(location_id, label)` candidates / the single best guess, using the app's
  `/api/locations.json` endpoint (website autocomplete as fallback).
- `search_metadata(category=None, *, category_id=None)` — the valid search
  filters for a category, as `{param: {label, type, search_param, values}}`
  (`values` lists allowed `(value, label)` pairs for ENUM params like
  `priceType`/`adType`). Mirrors the metadata the app uses to build its filter
  UI.
- `get_ad(ad_id)` — fetch a single ad. `fetch_categories()` — refresh the
  bundled category catalog from the live API.

## How the transport works

A plain `requests` client is blocked at the TLS layer, and the API expects app
headers. This client:

- impersonates a real Chrome TLS fingerprint via [`curl_cffi`](https://github.com/lexiforest/curl_cffi),
- sends the app version + a self-generated `X-EBAYK-APP` install id
  (a `uuid4` + millisecond timestamp, exactly how the app mints its own), and
- authenticates with the app's HTTP Basic credentials.

### Credentials & rotation

The Basic-auth username/password are **app-distribution values baked into the
Android client**, not personal secrets. They ship as defaults so `pip install`
just works — but Kleinanzeigen **can rotate them**. If you start getting
`401`/`403`, supply fresh values without editing the package:

```python
api = KleinanzeigenAPI(basic_user="…", basic_pw="…")
```

```bash
export KLEINANZEIGEN_BASIC_USER=…
export KLEINANZEIGEN_BASIC_PW=…
```

Resolution order: constructor arg → environment variable → bundled default.

## Legal & etiquette

- Kleinanzeigen's Terms of Service **forbid automated access**. This library is
  published for educational/personal use; **you are responsible** for how you
  use it. Don't scrape at scale, don't redistribute the data, and don't build
  anything that harms the service or its users.
- Be polite to the API: the client rate-limits to ~1.5 s/request by default
  (with jitter). Don't lower it much, and cache results instead of tight polling.
- No warranty — see [LICENSE](LICENSE).

## Development

```bash
git clone https://github.com/monkrel/kleinanzeigen-api
cd kleinanzeigen-api
pip install -e ".[dev]"
pytest -q          # offline parsing tests, no network
```

## ⭐ Like it?

If this helped you, a **star** means a lot — it's the only way I can tell the
tool is actually useful to people. Takes two seconds and keeps me motivated to
maintain it. Thank you! 🙏

## License

[MIT](LICENSE) © monkrel
