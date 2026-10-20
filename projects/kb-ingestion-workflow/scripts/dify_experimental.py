#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from integrations.dify_converter import execute_dify_experimental  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated dify-experimental conversion entrypoint."
    )
    parser.add_argument(
        "--selected-mode",
        choices=["dify-experimental"],
        required=True,
        help="Explicit mode selection required for the experimental Dify path.",
    )
    parser.add_argument("--request", type=Path, required=True, help="conversion-request v1 JSON file")
    parser.add_argument("--source-file", type=Path, required=True, help="sanitized local source file")
    parser.add_argument("--output", type=Path, default=None, help="optional conversion-result v1 JSON output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    request = json.loads(args.request.read_text(encoding="utf-8"))
    if isinstance(request, dict) and request.get("selected_mode") != args.selected_mode:
        request = dict(request)
        request["selected_mode"] = request.get("selected_mode", "")
    result = execute_dify_experimental(request, source_file=args.source_file)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if result.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
