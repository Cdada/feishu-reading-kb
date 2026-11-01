#!/usr/bin/env python3
"""Read the four-sample archive timings without starting any service."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from kb_custody_v21.archive_timing import report, markdown


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,help='Existing daily-service directory')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    value=report(Path(args.root)/'queue.sqlite3')
    print(json.dumps(value,ensure_ascii=False,indent=2) if args.json else markdown(value))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
