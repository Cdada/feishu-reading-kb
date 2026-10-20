#!/usr/bin/env python3
"""Scan staged blobs or commits not reachable from remote refs; print no matched values."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = {
    "private_home": re.compile(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "credential": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"),
    "platform_identifier": re.compile(rb"\b(?:cli_|ou_|oc_|om_)[a-f0-9]{16,}\b"),
    "private_ip": re.compile(rb"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
    "assigned_secret": re.compile(rb"(?i)(?:app_secret|access_token|refresh_token|api_key)\s*[:=]\s*[\"'][A-Za-z0-9_+/-]{24,}[\"']"),
}
FORBIDDEN = {".sqlite", ".sqlite3", ".db", ".log", ".zip", ".gz", ".pem", ".key"}


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    blobs = {}
    commits = []
    if args.history:
        commits = git("rev-list", "HEAD", "--not", "--remotes").decode().splitlines()
        # Compare every new commit to every parent, including merge parents.
        for commit in commits:
            parents = git("show", "-s", "--format=%P", commit).decode().split()
            for parent in parents or [git("hash-object", "-t", "tree", "--stdin").decode().strip()]:
                paths = git("diff", "--name-only", "--diff-filter=ACMRT", "-z", parent, commit).split(b"\0")
                for path in filter(None, paths):
                    name = path.decode()
                    oid = git("rev-parse", commit + ":" + name).decode().strip()
                    blobs[(oid, name)] = git("cat-file", "blob", oid)
    else:
        for path in filter(None, git("diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z").split(b"\0")):
            name = path.decode()
            oid = git("rev-parse", ":" + name).decode().strip()
            blobs[(oid, name)] = git("cat-file", "blob", oid)
    findings = []
    files = []
    for (oid, name), data in sorted(blobs.items()):
        files.append({"path": name, "blob": oid, "sha256": hashlib.sha256(data).hexdigest()})
        p = Path(name)
        if p.suffix in FORBIDDEN or any(part in {".dev-runtime", "node_modules", "backups", "credentials", "secrets"} for part in p.parts) or ".local." in p.name:
            findings.append({"path": name, "category": "forbidden_artifact"})
        if p.name.startswith(".env") and not p.name.endswith(".example"):
            findings.append({"path": name, "category": "environment_file"})
        for category, pattern in PATTERNS.items():
            for match in pattern.finditer(data):
                findings.append({"path": name, "category": category, "line": data[:match.start()].count(b"\n") + 1})
    print(json.dumps({"mode": "unpublished_history" if args.history else "index", "commits": commits,
                      "scanned_blobs": len(blobs), "files": files, "findings": findings,
                      "limitation": "Finite patterns plus manual file review; not proof of absence of all sensitive data."}, indent=2))
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
