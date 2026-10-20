#!/usr/bin/env python3
"""Local-only status/stop still work if standing rules expire or source changes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from kb_custody_v21.daily_launcher import DailyLauncher
from kb_custody_v21.daily_status import local_status,user_summary


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('status','stop'))
    parser.add_argument('--root',required=True)
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args(argv)
    if args.action=='stop':
        state=DailyLauncher(args.root).request_stop()
        value=local_status(args.root)
        value['stop_request']=state['state']
    else:
        value=local_status(args.root)
    print(json.dumps(value,ensure_ascii=False,indent=2) if args.json else user_summary(value))
    return 0


if __name__=='__main__':raise SystemExit(main())
