#!/usr/bin/env python3
"""Unified entry: python3 -I pow.py babel|parsec|hashcats ..."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from core.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
