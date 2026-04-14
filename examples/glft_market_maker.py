#!/usr/bin/env python3
"""Wrapper to run the GLFT market maker from the top-level examples/ directory.

Usage:
    cd py-blink-client
    set -a && source ../backend/.env && set +a
    python3 examples/glft_market_maker.py
"""
import os
import sys

# Ensure the package root is on sys.path
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from py_blink_client.examples.glft_market_maker import main

if __name__ == "__main__":
    main()
