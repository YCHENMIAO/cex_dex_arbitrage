"""
Backward-compatible import wrapper.

Use `models.py` for new code.
"""

from models import BookLevel, DEFAULT_BOOK_DEPTH, OrderBookSnapshot, utc_now_ms

__all__ = [
    "BookLevel",
    "DEFAULT_BOOK_DEPTH",
    "OrderBookSnapshot",
    "utc_now_ms",
]
