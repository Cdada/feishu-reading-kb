#!/usr/bin/env python3
"""One new F03 approval only; importing or running without --execute does nothing."""
import sys

from f02_execute import execute
from kb_custody_v21.f03 import ReconciledGroupExperiment


if __name__ == '__main__':
    if sys.argv[1:] != ['--execute']:
        raise SystemExit('explicit --execute and approved fixed F03 configuration required')
    try:
        sys.exit(execute(experiment_class=ReconciledGroupExperiment, reference='f03'))
    except Exception as error:
        print('STOPPED before execution setup: '+type(error).__name__)
        sys.exit(1)
