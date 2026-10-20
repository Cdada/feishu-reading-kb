#!/usr/bin/env python3
"""Check the complete Git index and optionally all reachable history; redact values."""
import argparse
import hashlib
import json
from pathlib import PurePosixPath
import re
import subprocess
import sys

# Only this reviewed synthetic picture may be committed as binary source.
REVIEWED_BINARIES = {'projects/kb-ingestion-workflow/integrations/kb_custody_v21/tests/fixtures/f02-geometry.png': '46c334467255ae9564c13280118776f5daf9d3985f7fb6bc183a8ed354a2fbf6'}
FORBIDDEN_SUFFIXES = {'.pdf', '.epub', '.docx', '.pptx', '.pages', '.webarchive',
                      '.sqlite', '.sqlite3', '.db', '.log', '.zip', '.gz', '.7z',
                      '.tar', '.dmg', '.pem', '.key', '.p12', '.pfx'}
FORBIDDEN_DIRS = {'.dev-runtime', 'node_modules', '.venv', 'backups', 'credentials',
                  'secrets', 'reading-library', 'local-state', 'raw_downloads'}
PATTERNS = {
    'personal_home': re.compile(r'(?:/(?:Users|home)/[\w.-]+(?:/|\b)|[A-Za-z]:[/\\]Users[/\\][\w.-]+)', re.I),
    'credential': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)'),
    'platform_identifier': re.compile(r'\b(?:cli_|ou_|oc_|om_)[a-f0-9]{16,}\b'),
    'assigned_secret': re.compile(r'''(?i)(?:app_secret|access_token|refresh_token|api_key)["']?\s*[:=]\s*["'][A-Za-z0-9_+/-]{24,}["']'''),
    'private_ip': re.compile(r'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
}
EMAIL = re.compile(r'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE)


def normalized(text):
    # Decode only common textual escapes, without altering UTF-8 Chinese.
    for _ in range(3):
        updated = re.sub(r'\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))',
                         lambda m: chr(int(m.group(1) or m.group(2), 16)), text)
        updated = updated.replace('\\/', '/')
        if updated == text:
            break
        text = updated
    return text


def permitted_email(email):
    domain = email.rsplit('@', 1)[-1].lower()
    return (domain in {'example.invalid', 'example.com', 'example.org', 'example.net', 'users.noreply.github.com', 'noreply.github.com'}
            or domain.endswith('.example.invalid'))


def text_findings(text, *, third_party_notice=False):
    text = normalized(text)
    found = []
    for category, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            found.append({'category': category, 'line': text[:match.start()].count('\n') + 1})
    if not third_party_notice:
        for match in EMAIL.finditer(text):
            if not permitted_email(match.group()):
                found.append({'category': 'personal_email', 'line': text[:match.start()].count('\n') + 1})
    return found


def check_blob(name, data, mode='100644'):
    path = PurePosixPath(normalized(name))
    found = []
    if mode not in {'100644', '100755'}:
        found.append({'category': 'symlink_or_submodule'})
    if (path.suffix.lower() in FORBIDDEN_SUFFIXES or '.local.' in path.name
            or set(path.parts) & FORBIDDEN_DIRS
            or (path.name.startswith('.env') and not path.name.endswith('.example'))):
        found.append({'category': 'private_artifact'})
    found.extend(text_findings(name))
    if name in REVIEWED_BINARIES and hashlib.sha256(data).hexdigest() == REVIEWED_BINARIES[name]:
        return found
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        found.append({'category': 'unreviewed_binary'})
        return found
    if '\x00' in text:
        found.append({'category': 'unreviewed_binary'})
        return found
    # Preserve public upstream copyright contacts in deliberately reviewed notices.
    found.extend(text_findings(text, third_party_notice=path.parts[0] == 'LICENSES'))
    return found


def inspect(history=False):
    findings, seen = [], set()
    def scan_tree(entries, source):
        for raw in filter(None, entries.split(b'\0')):
            metadata, name_bytes = raw.split(b'\t', 1)
            fields = metadata.decode().split()
            mode, oid = fields[0], fields[1] if source == 'index' else fields[2]
            name = name_bytes.decode('utf-8')
            key = (name, oid, mode)
            if key in seen:
                continue
            seen.add(key)
            if mode == '160000':
                items = [{'category': 'symlink_or_submodule'}]
            else:
                items = check_blob(name, git('cat-file', 'blob', oid), mode)
            findings.extend(dict(item, path=name, source=source) for item in items)
    scan_tree(git('ls-files', '--stage', '-z'), 'index')
    commits = git('rev-list', '--all').decode().splitlines() if history else []
    for commit in commits:
        raw = git('show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce%x00%B', commit).decode()
        author, email, committer, committer_email, message = raw.split('\0', 4)
        for role, value in (('author', email), ('committer', committer_email)):
            if not value.lower().endswith(('@users.noreply.github.com', '@noreply.github.com')):
                findings.append({'category': role + '_email_not_noreply', 'source': commit})
        findings.extend(dict(item, source=commit, path='<commit metadata>')
                        for item in text_findings('\n'.join((author, committer, message))))
        scan_tree(git('ls-tree', '-r', '-z', commit), commit)
    return {'mode': 'index_and_all_reachable_history' if history else 'complete_index',
            'scanned_blobs': len(seen), 'scanned_commits': len(commits), 'findings': findings,
            'limitation': 'Finite patterns and reviewed binary allowlist; manual review is still required.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true')
    args = parser.parse_args()
    try:
        result = inspect(args.history)
    except (subprocess.CalledProcessError, UnicodeError, ValueError) as error:
        print(json.dumps({'error': 'Unable to inspect complete Git content', 'type': type(error).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result['findings']))


if __name__ == '__main__':
    sys.exit(main())
