#!/usr/bin/env python3
"""Lanceur : permet `python main.py --reset-password <username>` comme
documenté, équivalent de `python -m jellyfin_stats.main`."""

import sys

from jellyfin_stats.main import main

if __name__ == "__main__":
    sys.exit(main())
