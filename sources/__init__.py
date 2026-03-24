"""Data source adapters for Handelsregister scraping."""

from .bundesapi import BundesAPISource
from .offeneregister import OffeneRegisterSource

__all__ = ["OffeneRegisterSource", "BundesAPISource"]
