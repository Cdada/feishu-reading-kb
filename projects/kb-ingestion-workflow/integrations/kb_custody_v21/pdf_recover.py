#!/usr/bin/env python3
"""Local operator repair. Does not start a service, conversion, or model call."""
import argparse
import json
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0,str(PACKAGE/'src'))
sys.path.insert(0,str(PACKAGE.parent/'local_conversion_mcp/src'))
from kb_custody_v21.daily_authority import DailyAuthority
from kb_custody_v21.daily_pdf_recovery import resume_repaired_pdf
from kb_custody_v21.daily_store import DailyStore
from kb_custody_v21.registry import Registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rules',required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--message-id',required=True)
    parser.add_argument('--operator-ref',required=True)
    parser.add_argument('--first-page',type=int)
    parser.add_argument('--raw-output',type=Path)
    args = parser.parse_args()
    authority = DailyAuthority(args.rules,args.sha256)
    store = DailyStore(authority.root/'queue.sqlite3',authority.policy)
    value = resume_repaired_pdf(authority,store,Registry(authority.runtime),args.message_id,
        operator_ref=args.operator_ref,first_page=args.first_page,raw_output=args.raw_output)
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
