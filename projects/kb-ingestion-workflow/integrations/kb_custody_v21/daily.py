#!/usr/bin/env python3
"""Portable entrypoint; use the reviewed local Python/dependency environment."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from kb_custody_v21.daily_cli import cli

if __name__=='__main__':
    raise SystemExit(cli())
