"""On-demand host metrics; no cgroups, privileged access, or metrics-server."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

METRICS_GROUP = {
    'name': 'metrics.k8s.io',
    'versions': [{'groupVersion': 'metrics.k8s.io/' + v, 'version': v} for v in ('v1', 'v1beta1')],
    'preferredVersion': {'groupVersion': 'metrics.k8s.io/v1', 'version': 'v1'},
}


def read_host(proc=Path('/proc')):
    fields = (proc / 'stat').read_text().splitlines()[0].split()
    if fields[0] != 'cpu' or len(fields) < 5:
        raise ValueError('invalid aggregate CPU counters')
    ticks = [int(x) for x in fields[1:]]
    if any(x < 0 for x in ticks):
        raise ValueError('negative CPU counter')
    # user/nice already include guest/guest_nice. Exclude idle and iowait.
    busy = sum(ticks[i] for i in (0, 1, 2, 5, 6, 7) if i < len(ticks))
    values = {}
    for line in (proc / 'meminfo').read_text().splitlines():
        key, _, value = line.partition(':')
        parts = value.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == 'kB' else 1)
    if not all(k in values for k in ('MemTotal', 'MemFree', 'Inactive(file)')):
        raise ValueError('memory working-set counters are unavailable')
    total = values['MemTotal']
    if total <= 0:
        raise ValueError('invalid total memory')
    memory = max(0, min(total, total - values['MemFree'] - values['Inactive(file)']))
    hz = os.sysconf('SC_CLK_TCK')
    if hz <= 0:
        raise ValueError('invalid CPU clock frequency')
    return busy / hz, memory, time.monotonic()


def read_visible_processes(proc=Path('/proc')):
    """Snapshot same-UID processes without reading aggregate host counters."""
    hz = os.sysconf('SC_CLK_TCK')
    pagesize = os.sysconf('SC_PAGE_SIZE')
    if hz <= 0 or pagesize <= 0:
        raise ValueError('invalid process accounting units')
    processes = {}
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            text = (path / 'stat').read_text()
            # comm may contain spaces and parentheses; fields after its final )
            # begin at field 3. starttime distinguishes PID reuse.
            fields = text[text.rindex(')') + 2:].split()
            cpu = (int(fields[11]) + int(fields[12])) / hz
            rss = max(0, int(fields[21])) * pagesize
            processes[(int(path.name), int(fields[19]))] = (cpu, rss)
        except (OSError, ValueError, IndexError):
            continue  # Processes can exit while /proc is being scanned.
    if not processes:
        raise ValueError('no readable same-user process counters')
    return processes, time.monotonic()



class HostMetrics:
    def __init__(self):
        self.scope = os.environ.get('POCKETKUBE_METRICS_SCOPE', 'host')
        if self.scope not in ('host', 'visible-processes'):
            raise ValueError('POCKETKUBE_METRICS_SCOPE must be host or visible-processes')
        self.lock = asyncio.Lock()
        self.cached = None
        self.updated = 0.0

    async def sample(self):
        async with self.lock:
            if self.cached is not None and time.monotonic() - self.updated < 1:
                return self.cached
            loop = asyncio.get_running_loop()
            try:
                if self.scope == 'visible-processes':
                    first, before = await loop.run_in_executor(None, read_visible_processes)
                    await asyncio.sleep(.25)
                    last, after = await loop.run_in_executor(None, read_visible_processes)
                    # Only stable PID/starttime pairs have a measured CPU delta.
                    used = sum(max(0, last[key][0] - first[key][0]) for key in first.keys() & last.keys())
                    memory = sum(value[1] for value in last.values())
                else:
                    start, _, before = await loop.run_in_executor(None, read_host)
                    await asyncio.sleep(.25)
                    end, memory, after = await loop.run_in_executor(None, read_host)
                    if end < start:
                        raise ValueError('CPU counters reset during sampling; retry the request')
                    used = end - start
                window = after - before
                if window <= 0:
                    raise ValueError('invalid CPU sampling interval')
                result = {
                    'timestamp': datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                    'window': f'{window:.9f}s',
                    'usage': {'cpu': str(round(used / window * 1_000_000_000)) + 'n',
                              'memory': str(memory)},
                }
            except (OSError, ValueError, IndexError, AttributeError) as exc:
                hint = ('; on restricted Android, opt in to same-user process metrics with '
                        'POCKETKUBE_METRICS_SCOPE=visible-processes (not whole-device usage)') if self.scope == 'host' else ''
                raise RuntimeError(self.scope + ' metrics unavailable: ' + str(exc) + hint) from exc
            self.updated = after
            self.cached = result
            return result
