"""Data source adapters for Handelsregister scraping."""

from .offeneregister import OffeneRegisterSource
from .bundesapi import BundesAPISource

__all__ = ['OffeneRegisterSource', 'BundesAPISource']
