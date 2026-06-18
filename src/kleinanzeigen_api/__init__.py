"""Unofficial Python client for the kleinanzeigen.de mobile JSON API.

This calls the real api.kleinanzeigen.de REST API used by the Android app of
Germany's Kleinanzeigen marketplace. It can search any category and returns
structured data the website doesn't show: GPS coordinates, exact result counts,
typed attributes, all image sizes, ISO timestamps and the price type.

Not affiliated with or endorsed by Kleinanzeigen GmbH / Adevinta. See the README
for the legal notes and rate-limiting advice.
"""
from __future__ import annotations

from .auth import Authenticator, NotLoggedIn
from .categories import Category, all_categories, find_categories, get_category
from .client import Conversation, KleinanzeigenAPI, Listing

__version__ = "0.3.0"
__all__ = [
    "KleinanzeigenAPI",
    "Listing",
    "Conversation",
    "Authenticator",
    "NotLoggedIn",
    "Category",
    "find_categories",
    "all_categories",
    "get_category",
    "__version__",
]
