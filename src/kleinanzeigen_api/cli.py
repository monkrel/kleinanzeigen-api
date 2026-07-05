"""Command-line interface. Run as `kleinanzeigen-api` or `python -m kleinanzeigen_api`."""
from __future__ import annotations

import argparse
import json
import sys

from .client import KleinanzeigenAPI, Listing


def _authed_client(rate: float = 1.5) -> KleinanzeigenAPI:
    """Build a client with the stored login, or exit telling the user to log in."""
    from .auth import Authenticator
    auth = Authenticator()
    if not auth.logged_in:
        print("Not logged in. Run:  kleinanzeigen-api login", file=sys.stderr)
        raise SystemExit(2)
    return KleinanzeigenAPI(rate_limit=rate, authenticator=auth)


def _cmd_login(argv) -> int:
    from .auth import Authenticator
    Authenticator().login_interactive()
    return 0


def _cmd_chats(argv) -> int:
    ap = argparse.ArgumentParser(prog="kleinanzeigen-api chats",
                                 description="List your chat threads.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    api = _authed_client()
    convs = api.conversations()
    if a.json:
        print(json.dumps([c.to_dict() for c in convs], ensure_ascii=False, indent=2))
    else:
        for c in convs:
            mark = "●" if c.unread else " "
            print(f"{mark} [{c.id}] {c.counterparty} — {c.ad_title[:50]}")
            if c.preview:
                print(f"      {c.preview[:80]}")
        print(f"{len(convs)} conversations", file=sys.stderr)
    return 0


def _cmd_reply(argv) -> int:
    ap = argparse.ArgumentParser(prog="kleinanzeigen-api reply",
                                 description="Reply in a chat thread.")
    ap.add_argument("conversation_id")
    ap.add_argument("message")
    a = ap.parse_args(argv)
    api = _authed_client()
    api.reply(a.conversation_id, a.message)
    print("sent", file=sys.stderr)
    return 0


def _cmd_messages(argv) -> int:
    ap = argparse.ArgumentParser(prog="kleinanzeigen-api messages",
                                 description="Show messages in a chat thread.")
    ap.add_argument("conversation_id")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    api = _authed_client()
    msgs = api.messages(a.conversation_id)
    if a.json:
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
    else:
        for m in msgs:
            who = "→ you" if m["direction"] == "received" else "you →"
            print(f"{who} [{m['date']}]: {m['text']}")
    return 0


def _cmd_my_ads(argv) -> int:
    ap = argparse.ArgumentParser(prog="kleinanzeigen-api my-ads",
                                 description="List your own ads.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    api = _authed_client()
    ads = api.my_ads()
    if a.json:
        print(json.dumps([l.to_dict() for l in ads], ensure_ascii=False, indent=2))
    else:
        _print_table(ads)
        print(f"{len(ads)} ads", file=sys.stderr)
    return 0


def _cmd_watchlist(argv) -> int:
    ap = argparse.ArgumentParser(prog="kleinanzeigen-api watchlist",
                                 description="List ads on your watchlist.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    api = _authed_client()
    ads = api.watchlist()
    if a.json:
        print(json.dumps([l.to_dict() for l in ads], ensure_ascii=False, indent=2))
    else:
        _print_table(ads)
        print(f"{len(ads)} saved ads", file=sys.stderr)
    return 0


def _cmd_post(argv) -> int:
    ap = argparse.ArgumentParser(prog="kleinanzeigen-api post",
                                 description="Post a new ad.")
    ap.add_argument("--title", required=True)
    ap.add_argument("--description", required=True)
    ap.add_argument("--category", required=True, help="category id")
    ap.add_argument("--location", required=True,
                    help="specific postable location (name/postcode or numeric id)")
    ap.add_argument("--price", type=int, help="price in euro (omit for FREE)")
    ap.add_argument("--price-type", default="FIXED",
                    choices=["FIXED", "NEGOTIABLE", "FREE"])
    ap.add_argument("--contact-name")
    ap.add_argument("--email", help="contact email (defaults to your account email)")
    a = ap.parse_args(argv)
    api = _authed_client()
    loc = a.location if str(a.location).isdigit() else None
    if loc is None:
        best = api.best_location(a.location)
        if not best:
            print(f"could not resolve location {a.location!r}", file=sys.stderr)
            return 2
        loc = best[0]
    new_id = api.post_ad(title=a.title, description=a.description,
                         category_id=a.category, location_id=loc, price=a.price,
                         price_type=a.price_type, contact_name=a.contact_name,
                         email=a.email)
    print(new_id or "(posted, id unknown)")
    print(f"posted ad {new_id}", file=sys.stderr)
    return 0


# pause / activate / delete / extend all take just an ad id, so they share this.
def _ad_id_arg(argv, name):
    ap = argparse.ArgumentParser(prog=f"kleinanzeigen-api {name}")
    ap.add_argument("ad_id")
    return ap.parse_args(argv).ad_id


def _cmd_pause(argv) -> int:
    ad_id = _ad_id_arg(argv, "pause")
    _authed_client().pause_ad(ad_id)
    print(f"paused {ad_id}", file=sys.stderr)
    return 0


def _cmd_activate(argv) -> int:
    ad_id = _ad_id_arg(argv, "activate")
    _authed_client().activate_ad(ad_id)
    print(f"activated {ad_id}", file=sys.stderr)
    return 0


def _cmd_delete(argv) -> int:
    ad_id = _ad_id_arg(argv, "delete")
    _authed_client().delete_ad(ad_id)
    print(f"deleted {ad_id}", file=sys.stderr)
    return 0


def _cmd_extend(argv) -> int:
    ad_id = _ad_id_arg(argv, "extend")
    _authed_client().extend_ad(ad_id)
    print(f"extended {ad_id}", file=sys.stderr)
    return 0


def _cmd_watch(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="kleinanzeigen-api watch",
        description="Stream newly-posted ads as they appear. By default this "
                    "polls search in a way that dodges the site's ~2-minute "
                    "result cache, so new ads show up ~15-45s after posting at "
                    "just a few requests per minute. With --fast it reads every "
                    "new ad by id instead - that sees ads within seconds, but "
                    "needs ~15-20 requests per SECOND to keep up, so save it "
                    "for short, narrow watches.")
    ap.add_argument("--fast", action="store_true",
                    help="fast mode: fetch ads by id the moment they're posted "
                         "(heavy on requests; needs a very low --rate)")
    ap.add_argument("--category", metavar="NAME_OR_ID",
                    help="restrict to a category by name or id")
    ap.add_argument("--q", help="keyword(s) to match")
    ap.add_argument("--exclude", action="append", metavar="TERM",
                    help="drop ads containing TERM (repeatable or comma-separated)")
    ap.add_argument("--min-price", type=float)
    ap.add_argument("--max-price", type=float)
    ap.add_argument("--price-type", choices=["free", "fixed", "negotiable"],
                    help="free = zu verschenken")
    ap.add_argument("--poster-type", choices=["private", "commercial"])
    ap.add_argument("--location", metavar="NAME_OR_ID",
                    help="place name or location id, e.g. Berlin (default mode only)")
    ap.add_argument("--distance", type=float, metavar="KM",
                    help="radius in km around --location")
    ap.add_argument("--near", metavar="LAT,LON",
                    help="center point for a radius filter, e.g. 52.52,13.405")
    ap.add_argument("--radius", type=float, metavar="KM",
                    help="max distance in km from --near (needs --near)")
    ap.add_argument("--backfill", action="store_true",
                    help="also emit the current batch of ads before going live")
    ap.add_argument("--interval", type=float, default=15.0,
                    help="seconds between checks once caught up (default 15)")
    ap.add_argument("--rate", type=float, default=None,
                    help="min seconds between requests (default: 1.5, or 0.05 "
                         "with --fast)")
    ap.add_argument("--json", action="store_true", help="emit one JSON object per line")
    a = ap.parse_args(argv)

    near = None
    if a.near:
        try:
            lat, lon = (float(x) for x in a.near.split(","))
            near = (lat, lon)
        except ValueError:
            print("--near must look like LAT,LON, e.g. 52.52,13.405", file=sys.stderr)
            return 2
    if a.radius is not None and near is None:
        print("--radius needs --near", file=sys.stderr)
        return 2
    if a.distance is not None and not a.location:
        print("--distance needs --location", file=sys.stderr)
        return 2
    if a.fast and a.location:
        print("--location only works in the default mode; with --fast use "
              "--near LAT,LON --radius KM instead", file=sys.stderr)
        return 2

    category_id = None
    if a.category:
        from .categories import resolve_category
        category_id = resolve_category(a.category)

    exclude = []
    for chunk in (a.exclude or []):
        exclude.extend(part.strip() for part in chunk.split(",") if part.strip())

    price_type = {"free": "FREE", "fixed": "SPECIFIED_AMOUNT",
                  "negotiable": "PLEASE_CONTACT"}.get(a.price_type)

    mode = "frontier" if a.fast else "search"
    rate = a.rate if a.rate is not None else (0.05 if a.fast else 1.5)
    api = KleinanzeigenAPI(rate_limit=rate)
    print("watching for new ads… (Ctrl-C to stop)", file=sys.stderr)
    try:
        for l in api.iter_new_ads(
                mode=mode, backfill=a.backfill, category_id=category_id,
                q=a.q, exclude=exclude or None, min_price=a.min_price,
                max_price=a.max_price, price_type=price_type,
                poster_type=(a.poster_type or "").upper() or None,
                location=a.location, distance_km=a.distance,
                near=near, radius_km=a.radius, poll_interval=a.interval):
            if a.json:
                print(json.dumps(l.to_dict(), ensure_ascii=False), flush=True)
            else:
                price = f"{int(l.price)} €" if l.price else (l.price_type or "—")
                loc = f"{l.zip_code} {l.city}".strip() or "—"
                print(f"[{l.id}] {price:>9} | {loc}  {l.title[:70]}", flush=True)
                print(f"    {l.url}", flush=True)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


_SUBCOMMANDS = {"login": _cmd_login, "chats": _cmd_chats, "reply": _cmd_reply,
                "messages": _cmd_messages, "my-ads": _cmd_my_ads,
                "watchlist": _cmd_watchlist, "post": _cmd_post,
                "pause": _cmd_pause, "activate": _cmd_activate,
                "delete": _cmd_delete, "extend": _cmd_extend,
                "watch": _cmd_watch}


def _print_table(items: list) -> None:
    """Print the listings as a short text table."""
    for l in items:
        price = f"{int(l.price)} €" if l.price else (l.price_type or "—")
        loc = f"{l.zip_code} {l.city}".strip() or "—"
        extra = []
        if l.rooms:
            extra.append(f"{l.rooms:g} Zi")
        if l.size_m2:
            extra.append(f"{l.size_m2:g} m²")
        nk = l.attributes.get("Warmmiete")
        if nk:
            extra.append(f"warm {nk}€")
        tail = (" | " + " · ".join(extra)) if extra else ""
        print(f"[{l.id}] {price:>9} | {loc}{tail}")
        print(f"    {l.title[:90]}")
        print(f"    {l.url}")
        print()


def main(argv=None) -> int:
    """Parse the command-line arguments, run the search, and print the results.

    Returns the process exit code (0 on success, 2 on a bad argument or an
    API error).
    """
    args_list = list(sys.argv[1:] if argv is None else argv)
    # if the first word is one of our logged-in commands (login, chats, post, …)
    # run that; otherwise fall through to the normal search below.
    if args_list and args_list[0] in _SUBCOMMANDS:
        try:
            return _SUBCOMMANDS[args_list[0]](args_list[1:])
        except (RuntimeError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    p = argparse.ArgumentParser(
        prog="kleinanzeigen-api",
        description="Unofficial search client for kleinanzeigen.de (Germany). "
                    "Searches all categories by default.")
    p.add_argument("location", nargs="?", help="city/region name or numeric location id")
    p.add_argument("--category", default=None, metavar="NAME_OR_ID",
                   help="restrict to a category by NAME or id (default: all categories; "
                        "e.g. \"Fahrräder & Zubehör\" or 217). Browse with --categories.")
    p.add_argument("--categories", nargs="?", const="", metavar="QUERY",
                   help="list category ids matching QUERY (or all of them) and exit")
    p.add_argument("--distance", type=int, help="radius km")
    p.add_argument("--min-price", type=int)
    p.add_argument("--max-price", type=int)
    p.add_argument("--min-rooms", type=float)
    p.add_argument("--max-rooms", type=float)
    p.add_argument("--min-size", type=float)
    p.add_argument("--max-size", type=float)
    p.add_argument("--q", help="keyword (server-side)")
    p.add_argument("--exclude", action="append", metavar="TERM",
                   help="drop results containing TERM in title/description "
                        "(repeatable, or comma-separated; client-side)")
    p.add_argument("--ad-type", choices=["offered", "wanted"], default="offered",
                   help="OFFERED listings (default) or WANTED ads")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--size", type=int, default=25, help="results per page (max ~25)")
    p.add_argument("--sort", choices=["new", "cheap", "expensive", "near"],
                   help="server-side sort: new=newest, cheap/expensive=price, near=distance")
    p.add_argument("--sort-price", action="store_true", help="alias for --sort cheap")
    p.add_argument("--rate", type=float, default=1.5, help="min seconds between requests")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument("--out", help="write JSON to this file")
    args = p.parse_args(argv)

    # --categories: list categories from the bundled file and exit (no network)
    if args.categories is not None:
        from .categories import all_categories, find_categories
        cats = find_categories(args.categories) if args.categories else all_categories()
        for c in cats:
            flag = "  [real estate]" if c.real_estate else ""
            print(f"{c.id:>5}  {c.path}{flag}")
        if args.categories and not cats:
            print(f"no categories match {args.categories!r}", file=sys.stderr)
        return 0

    sort_map = {"new": "DATE_DESCENDING", "cheap": "PRICE_ASCENDING",
                "expensive": "PRICE_DESCENDING", "near": "DISTANCE_ASCENDING"}
    sort_type = sort_map.get(args.sort) or ("PRICE_ASCENDING" if args.sort_price else None)

    exclude = []
    for chunk in (args.exclude or []):
        exclude.extend(part.strip() for part in chunk.split(",") if part.strip())

    api = KleinanzeigenAPI(rate_limit=args.rate)
    try:
        items = api.search(
            location=args.location, q=args.q, exclude=exclude or None,
            category=args.category, distance_km=args.distance,
            min_price=args.min_price, max_price=args.max_price,
            min_rooms=args.min_rooms, max_rooms=args.max_rooms,
            min_size=args.min_size, max_size=args.max_size,
            ad_type=args.ad_type.upper(),
            sort_type=sort_type, pages=args.pages, size=args.size)
    except (ValueError, RuntimeError) as e:
        # ValueError: unknown/ambiguous category or location.
        # RuntimeError: rotated credentials (401/403) or network failure from the API.
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.json or args.out:
        text = json.dumps([l.to_dict() for l in items], ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"wrote {len(items)} listings -> {args.out}", file=sys.stderr)
        else:
            print(text)
    else:
        _print_table(items)
        print(f"{len(items)} listings", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
