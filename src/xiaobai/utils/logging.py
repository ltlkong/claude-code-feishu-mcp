"""Logging helper — thin wrapper around ``logging.getLogger``.

Currently just ``get_logger(name)``. Centralized so future changes (structured
logging, handlers, log filtering) have one place to live.
"""

from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    """Return a logger named ``name``; callers typically pass ``__name__``."""
    return logging.getLogger(name)
