"""Pod log options and bounded, independently followable PRoot log history."""
from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


@dataclass
class LogOptions:
    follow: bool = False
    timestamps: bool = False
    tail: int = -1
    since: float | None = None
    limit: int | None = None

    @classmethod
    def parse(cls, params):
        def boolean(name):
            value = params.get(name, 'false').lower()
            if value not in ('true', 'false', '1', '0'):
                raise ValueError(name + ' must be a boolean')
            return value in ('true', '1')
        def integer(name, default, minimum):
            if name not in params:
                return default
            value = params[name]
            if not re.fullmatch(r'-?[0-9]+', value):
                raise ValueError(name + ' must be an integer')
            value = int(value)
            if value < minimum:
                raise ValueError(name + ' must be at least ' + str(minimum))
            return value
        if boolean('previous'):
            raise ValueError('previous container logs are unavailable; container restarts are not supported')
        if params.get('stream', 'All') != 'All':
            raise ValueError('only combined stdout/stderr logs (stream=All) are supported')
        if 'sinceSeconds' in params and 'sinceTime' in params:
            raise ValueError('sinceSeconds and sinceTime cannot both be specified')
        seconds = integer('sinceSeconds', None, 1)
        since = time.time() - seconds if seconds is not None else None
        if 'sinceTime' in params:
            value = params['sinceTime']
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
                raise ValueError('sinceTime must be an RFC3339 timestamp with timezone')
            since = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return cls(boolean('follow'), boolean('timestamps'), integer('tailLines', -1, -1),
                   since, integer('limitBytes', None, 1))


async def limit_stream(stream, limit):
    """Close the underlying follower on completion, byte limit, or disconnect."""
    try:
        async for chunk in stream:
            if limit is not None:
                chunk = chunk[:limit]
                limit -= len(chunk)
            if chunk:
                yield chunk
            if limit == 0:
                return
    finally:
        await stream.aclose()


class LogBuffer:
    def __init__(self, max_bytes=1024 * 1024, max_records=16384):
        self.records = deque()
        self.max_bytes = max_bytes
        self.max_records = max_records
        self.size = 0
        self.sequence = 0
        self.line = 0
        self.line_time = None
        self.closed = False
        self.changed = asyncio.Event()

    def append(self, chunk, now=None):
        if self.closed:
            return
        now = time.time() if now is None else now
        # Split only on LF; preserve carriage returns, binary data and partial lines.
        parts = chunk.split(b'\n')
        for index, part in enumerate(parts):
            if index < len(parts) - 1:
                part += b'\n'
            if not part:
                continue
            if self.line_time is None:
                self.line_time = now
            # Bound even a single huge unterminated line.
            for offset in range(0, len(part), min(8192, self.max_bytes)):
                data = part[offset:offset + min(8192, self.max_bytes)]
                self.records.append((self.sequence, self.line, self.line_time, data))
                self.sequence += 1
                self.size += len(data)
            if part.endswith(b'\n'):
                self.line += 1
                self.line_time = None
            while self.size > self.max_bytes or len(self.records) > self.max_records:
                self.size -= len(self.records.popleft()[3])
        self.changed.set()

    def close(self):
        self.closed = True
        self.changed.set()

    async def stream(self, options):
        cursor = self.sequence
        records = list(self.records)
        if options.tail == 0:
            records = []
        elif options.tail > 0 and records:
            first_line = records[-1][1] - options.tail + 1
            records = [r for r in records if r[1] >= first_line]
        last_line = None
        while True:
            for sequence, line, when, data in records:
                if options.since is not None and when < options.since:
                    continue
                if options.timestamps and line != last_line:
                    yield timestamp(when).encode() + b' ' + data
                else:
                    yield data
                last_line = line
            if not options.follow:
                return
            # No await between clearing the event and snapshotting: appends
            # cannot get lost between history and live output on this event loop.
            self.changed.clear()
            records = [r for r in self.records if r[0] >= cursor]
            cursor = self.sequence
            if records:
                continue
            if self.closed:
                return
            await self.changed.wait()
