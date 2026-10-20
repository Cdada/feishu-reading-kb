"""Read-only macOS admission for Pilot heavy work; no process termination.

The pressure sysctl returns dispatch NOTE_MEMORYSTATUS values (1/2/4), not
the internal kernel enum. Unknown or unreadable probes pause admission.
"""
from dataclasses import dataclass
import json
import re
import shutil
import subprocess
import sys

from .contracts import ContractError, stable_key
from .storage_layout import StorageUnavailable


GIB = 1024 ** 3


class ResourceUnavailable(Exception):
    """A known OS probe failure with a fixed, non-sensitive reason."""


@dataclass(frozen=True)
class ResourceSample:
    free_percent: int
    pressure: str
    disk_free_bytes: int

    def validate(self):
        if (type(self.free_percent) is not int or not 0 <= self.free_percent <= 100
                or self.pressure not in {'normal', 'warning', 'critical'}
                or type(self.disk_free_bytes) is not int or self.disk_free_bytes < 0):
            raise ContractError('invalid resource sample')


def parse_memory(free_output, pressure_output, disk_free_bytes):
    matches = re.findall(r'^System-wide memory free percentage: ([0-9]{1,3})%$',
                         free_output, re.MULTILINE)
    pressures = {'1': 'normal', '2': 'warning', '4': 'critical'}
    if len(matches) != 1 or pressure_output.strip() not in pressures or int(matches[0]) > 100:
        raise ResourceUnavailable('memory_probe_invalid')
    sample = ResourceSample(int(matches[0]), pressures[pressure_output.strip()], disk_free_bytes)
    sample.validate()
    return sample


def macos_sample(root):
    if sys.platform != 'darwin':
        raise ResourceUnavailable('resource_probe_unsupported')
    try:
        def output(args):
            return subprocess.run(args, capture_output=True, text=True, check=True,
                                  timeout=5, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}).stdout
        # -Q queries current status. Never invoke simulation or pressure-generation flags.
        free = output(['/usr/bin/memory_pressure', '-Q'])
        pressure = output(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level'])
        disk = shutil.disk_usage(root).free
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ResourceUnavailable('resource_probe_unavailable') from exc
    return parse_memory(free, pressure, disk)


class PilotResources:
    """Persist pause/resume transitions in the existing custody event log."""
    def __init__(self, registry, *, probe=None, minimum_free_bytes=5 * GIB):
        if type(minimum_free_bytes) is not int or minimum_free_bytes < 5 * GIB:
            raise ContractError('resource disk floor cannot be below 5 GiB')
        self.registry, self.probe = registry, probe if probe is not None else macos_sample
        self.minimum_free_bytes = minimum_free_bytes

    def check(self):
        try:
            storage = self.registry.runtime.check_storage()
            sample = self.probe(self.registry.runtime.root)
            sample.validate()
            if storage and storage['free_bytes'] < self.minimum_free_bytes:
                raise StorageUnavailable('insufficient_space')
            unavailable = None
        except StorageUnavailable as error:
            sample, unavailable = None, 'storage_'+error.reason
        except ResourceUnavailable:
            # Do not log OS output, paths, stderr or arbitrary adapter text.
            sample, unavailable = None, 'resource_probe_unavailable'
        with self.registry.transaction() as db:
            previous = db.execute("""SELECT event_id,payload_json FROM custody_events
                WHERE event_type='pilot_resource_admission' AND aggregate_id='pilot-resources'
                ORDER BY rowid DESC LIMIT 1""").fetchone()
            old = json.loads(previous['payload_json']) if previous else None
            paused = old is not None and old['state'] == 'paused_resources'
            reason = unavailable
            if sample:
                if sample.pressure != 'normal':
                    reason = 'memory_pressure_' + sample.pressure
                elif sample.free_percent < (25 if paused else 20):
                    reason = 'memory_recovery_threshold' if paused else 'memory_free_low'
                elif sample.disk_free_bytes < self.minimum_free_bytes:
                    reason = 'disk_free_low'
            result = dict(state='paused_resources' if reason else 'resources_ready',
                          reason=reason, free_percent=sample.free_percent if sample else None,
                          pressure=sample.pressure if sample else None,
                          disk_free_bytes=sample.disk_free_bytes if sample else None)
            if old is None or (old['state'], old['reason']) != (result['state'], result['reason']):
                self.registry.event(db, 'pilot_resource_admission', 'pilot-resources',
                    stable_key('pilot-resources', previous['event_id'] if previous else 'initial',
                               result['state'], result['reason']), result)
            return result
