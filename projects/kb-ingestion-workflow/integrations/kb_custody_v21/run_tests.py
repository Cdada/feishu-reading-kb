#!/usr/bin/env python3
"""Fail closed at the DEV gate, then emit actual unittest JSON/XML/log evidence."""
import io
import os
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
import xml.etree.ElementTree as ET

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / "src"))
sys.path.insert(0, str(PACKAGE.parent / 'local_conversion_mcp/src'))
from kb_custody_v21.runtime import Runtime
from dev import offline_guard


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def record(self, test, status, detail=""):
        self.records.append({"test": test.id(), "description": test.shortDescription(),
                             "status": status, "seconds": time.monotonic() - self.started, "detail": detail,
                             "runtime": str(test.runtime.root) if hasattr(test, "runtime") else None})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "error", self._exc_info_to_string(err, test))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(subtest, "failed", self._exc_info_to_string(err, test))


def main():
    worktree = Path(subprocess.check_output(["git", "-C", str(PACKAGE), "rev-parse", "--show-toplevel"], text=True).strip())
    runtime = Runtime.load(worktree, PACKAGE / "dev.example.json")
    gate = runtime.initialize()  # Must pass BEFORE discovery or running test code.
    sys.addaudithook(offline_guard)
    os.environ['KB_V21_OFFLINE_TEST'] = '1'
    os.environ['PYTHONPATH'] = str(PACKAGE / 'tests/offline_site') + os.pathsep + os.environ.get('PYTHONPATH', '')
    output = runtime.root / "test-output"
    (output / "safety-gate.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2))
    suite = unittest.defaultTestLoader.discover(str(PACKAGE / "tests"))
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=RecordedResult).run(suite)
    rendered = log.getvalue()
    (output / "unittest.log").write_text(rendered)
    report = {"python": sys.version, "tests_run": result.testsRun, "successful": result.wasSuccessful(),
              "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
              "network": "blocked by audit hook in runner, DEV CLI and inherited Python subprocesses", "records": result.records}
    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    xml = ET.Element("testsuite", tests=str(result.testsRun), failures=str(len(result.failures)), errors=str(len(result.errors)))
    for row in result.records:
        case = ET.SubElement(xml, "testcase", name=row["test"], time=str(row["seconds"]))
        if row["status"] != "passed":
            ET.SubElement(case, "failure").text = row["detail"]
    ET.ElementTree(xml).write(output / "junit.xml", encoding="utf-8", xml_declaration=True)
    print(rendered)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
