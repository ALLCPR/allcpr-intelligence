"""Spam/scam Google Maps listings must never rank as viable anchors.

Surfaced by a live San Jose run where "free amazon gift card generator"
(a fake listing) ranked #1. The filter must block obvious scams while NOT
false-positiving legitimate businesses with words like "free" or "generator".
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.utils.viability_filter import is_anchor_viable  # noqa: E402


SPAM = [
    "free amazon gift card generator",
    "Robux Generator",
    "Free V-Bucks no survey",
    "Make Money Fast Online",
    "100% Free iTunes Codes",
    "PSN Code Generator",
    "Bitcoin giveaway free",
    "Free Steam Wallet Codes",
    "Unlimited Coins Generator",
]

LEGIT = [
    "Free People",
    "Generator Coffee House",
    "Bitcoin Depot ATM",
    "Gift Box Boutique",
    "The Coin Laundry",
    "Five Guys",
    "Gems Nail Spa",
    "Liberty Tax Service",
    "24 Hour Fitness",
    "Doc's Office",
    "Cash & Carry Wholesale",
]


@pytest.mark.parametrize("name", SPAM)
def test_spam_listings_are_blocked(name):
    viable, reason = is_anchor_viable(types=["establishment"], name=name)
    assert viable is False, f"spam not blocked: {name!r}"
    assert "spam" in reason.lower()


@pytest.mark.parametrize("name", LEGIT)
def test_legitimate_names_are_not_false_positived(name):
    viable, _ = is_anchor_viable(types=["establishment"], name=name)
    assert viable is True, f"legit business wrongly blocked: {name!r}"
