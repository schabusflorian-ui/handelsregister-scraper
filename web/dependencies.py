"""
Shared dependencies for the web application.

Contains database access, templates, and helper functions
used across all router modules.
"""

import json as _json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi.templating import Jinja2Templates

from persistence.database import Database

# Configuration
DB_PATH = os.environ.get("DB_PATH", "handelsregister.db")
WEB_DIR = Path(__file__).parent


def get_db() -> Database:
    """Get database connection."""
    return Database(DB_PATH)


def format_currency(amount: Optional[float], currency: str = "EUR") -> str:
    """Format currency amount."""
    if amount is None:
        return "-"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M {currency}"
    elif amount >= 1_000:
        return f"{amount / 1_000:.0f}K {currency}"
    return f"{amount:,.0f} {currency}"


def format_date(date_str: Optional[str]) -> str:
    """Format date string."""
    if not date_str:
        return "-"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except:
        return date_str[:10] if len(date_str) >= 10 else date_str


# Templates
templates = Jinja2Templates(directory=WEB_DIR / "templates")

# Add template filters
templates.env.filters["currency"] = format_currency
templates.env.filters["date"] = format_date
templates.env.filters["split"] = lambda s, sep=",": s.split(sep) if s else []
templates.env.filters["from_json"] = lambda s: _json.loads(s) if s else []
