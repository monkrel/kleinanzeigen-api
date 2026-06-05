"""Command-line interface. Run as `kleinanzeigen-api` or `python -m kleinanzeigen_api`."""
from __future__ import annotations

import argparse
import json
import sys

from .client import KleinanzeigenAPI, Listing


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
