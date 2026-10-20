"""Synthetic process-death probe; portable imports and explicit offline guard."""
import os
from pathlib import Path
import sys

PACKAGE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PACKAGE),str(PACKAGE/'src'),str(PACKAGE/'tests'),str(PACKAGE.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
from test_pilot_intake import PilotIntake, Registry, Runtime, DEFAULTS, WORKTREE, SCOPE

flow=PilotIntake(Registry(Runtime(WORKTREE,DEFAULTS,Path(sys.argv[1]))),SCOPE,max_attachments=5,max_pending=8)
flow._bind=lambda *_: os._exit(86)
flow.work_once()
