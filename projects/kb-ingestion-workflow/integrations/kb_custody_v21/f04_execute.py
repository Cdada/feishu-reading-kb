#!/usr/bin/env python3
"""Known-message F04 continuation; importing cannot call the platform."""
import sys

from f02_execute import execute
from kb_custody_v21.f04 import KnownMessageExperiment


if __name__ == '__main__':
    if sys.argv[1:] != ['--execute']:
        raise SystemExit('explicit --execute and approved fixed F04 configuration required')
    try:
        sys.exit(execute(experiment_class=KnownMessageExperiment, reference='f04', reuse_known_txt=True))
    except Exception as error:
        print('STOPPED before execution setup: '+type(error).__name__)
        sys.exit(1)
