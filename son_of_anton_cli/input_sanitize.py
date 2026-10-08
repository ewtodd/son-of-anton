"""Sanitize user prompt text leaked from terminal / paste control sequences."""

from __future__ import annotations



# Corruption signature from desktop bracketed-paste leaks (#62557).


