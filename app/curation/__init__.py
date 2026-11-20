"""Curator support code (ADR-0002).

Nothing in this package imports FastAPI: the curator runs from the CLI and
from ``web_api`` alike, so shared logic lives here and ``web_api`` imports it.
"""
