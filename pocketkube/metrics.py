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


class HostMetrics:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.cached = None
        self.updated = 0.0

    async def sample(self):
        async with self.lock:
            if self.cached is not None and time.monotonic() - self.updated < 1:
                return self.cached
            loop = asyncio.get_running_loop()
            try:
                start, _, before = await loop.run_in_executor(None, read_host)
                await asyncio.sleep(.25)
                end, memory, after = await loop.run_in_executor(None, read_host)
                window = after - before
                if window <= 0 or end < start:
                    raise ValueError('CPU counters reset during sampling; retry the request')
                result = {
                    'timestamp': datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                    'window': f'{window:.9f}s',
                    'usage': {'cpu': str(round((end - start) / window * 1_000_000_000)) + 'n',
                              'memory': str(memory)},
                }
            except (OSError, ValueError, IndexError, AttributeError) as exc:
                raise RuntimeError('host metrics unavailable: ' + str(exc)) from exc
            self.updated = after
            self.cached = result
            return result
