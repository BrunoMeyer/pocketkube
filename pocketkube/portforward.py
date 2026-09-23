"""Kubectl's SPDY/3.1 portforward streams carried over binary WebSockets.

This is the Kubernetes streaming subset, not an HTTP SPDY server. As in
moby/spdystream, TCP/WebSocket backpressure supplies connection flow control.
"""
from __future__ import annotations

import asyncio
import struct
import zlib

import anyio
from starlette.websockets import WebSocketDisconnect

from .spdy_dictionary import DICTIONARY

PROTOCOLS = ('SPDY/3.1+portforward.k8s.io', 'v2.portforward.k8s.io')
MAX_FRAME = 1024 * 1024
MAX_HEADERS = 65536
MAX_CONNECTIONS = 64
MAX_PENDING = 256 * 1024


def frame(kind, payload=b'', flags=0, stream=0):
    first = 0x80030000 | kind if kind else stream
    return struct.pack('!II', first, flags << 24 | len(payload)) + payload


class Headers:
    def __init__(self):
        self.decoder = zlib.decompressobj(zdict=DICTIONARY)
        self.encoder = zlib.compressobj(zdict=DICTIONARY)

    def decode(self, block):
        data = self.decoder.decompress(block, MAX_HEADERS + 1)
        if len(data) > MAX_HEADERS or self.decoder.unconsumed_tail:
            raise ValueError('portforward headers too large')
        offset = 0
        def integer():
            nonlocal offset
            if offset + 4 > len(data):
                raise ValueError('truncated portforward headers')
            value = struct.unpack_from('!I', data, offset)[0]
            offset += 4
            return value
        def string():
            nonlocal offset
            length = integer()
            if offset + length > len(data):
                raise ValueError('truncated portforward header value')
            value = data[offset:offset + length].decode('utf-8')
            offset += length
            return value
        count = integer()
        if count > 64:
            raise ValueError('too many portforward headers')
        result = {}
        for _ in range(count):
            key, value = string().lower(), string()
            if key in result or '\x00' in value:
                raise ValueError('duplicate portforward header')
            result[key] = value
        if offset != len(data):
            raise ValueError('invalid portforward headers')
        return result

    def encode(self, headers):
        data = struct.pack('!I', len(headers))
        for key, value in headers.items():
            for text in (key, value):
                encoded = text.encode()
                data += struct.pack('!I', len(encoded)) + encoded
        return self.encoder.compress(data) + self.encoder.flush(zlib.Z_SYNC_FLUSH)


class Forward:
    def __init__(self, port):
        self.port = port
        self.streams = {}
        self.ready = asyncio.Event()
        self.queue = asyncio.Queue()
        self.pending = 0
        self.capacity = asyncio.Event()
        self.eof = False
        self.task = None
        self.writer = None


class PortForwardSession:
    def __init__(self, ws, connect):
        self.ws = ws
        self.connect = connect
        self.headers = Headers()
        self.send_lock = asyncio.Lock()
        self.streams = {}
        self.connections = {}
        self.tasks = set()
        self.last_stream = 0
        self.closing = False

    async def send(self, data):
        async with self.send_lock:
            if not self.closing:
                await asyncio.wait_for(self.ws.send_bytes(data), 10)

    async def data(self, stream, data=b'', end=False):
        await self.send(frame(0, data, int(end), stream))

    async def forward(self, request_id, connection):
        children = []
        try:
            await asyncio.wait_for(connection.ready.wait(), 30)
            reader, writer = await asyncio.wait_for(self.connect(connection.port), 10)
            connection.writer = writer
            stream = connection.streams['data']
            async def upload():
                while True:
                    data = await connection.queue.get()
                    if data is None:
                        if writer.can_write_eof():
                            writer.write_eof()
                            await asyncio.wait_for(writer.drain(), 10)
                        return
                    connection.pending -= len(data)
                    connection.capacity.set()
                    writer.write(data)
                    await asyncio.wait_for(writer.drain(), 10)
            async def download():
                while True:
                    data = await reader.read(32768)
                    if not data:
                        return
                    await self.data(stream, data)
            upload_task = asyncio.create_task(upload())
            download_task = asyncio.create_task(download())
            children = [upload_task, download_task]
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if download_task not in done:
                await download_task
        except (OSError, RuntimeError, ValueError, WebSocketDisconnect, asyncio.TimeoutError) as exc:
            error = connection.streams.get('error')
            if error is not None:
                message = f'cannot forward port {connection.port}: {exc or "connection timed out"}'
                try:
                    await self.data(error, message.encode())
                except (OSError, RuntimeError, WebSocketDisconnect, asyncio.TimeoutError):
                    pass
        finally:
            for task in children:
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*children, return_exceptions=True)
                if connection.writer:
                    connection.writer.close()
                    try:
                        await asyncio.wait_for(connection.writer.wait_closed(), 1)
                    except (OSError, asyncio.TimeoutError):
                        pass
                for stream in connection.streams.values():
                    self.streams.pop(stream, None)
                    try:
                        await self.data(stream, end=True)
                    except (OSError, RuntimeError, WebSocketDisconnect, asyncio.TimeoutError):
                        pass
                self.connections.pop(request_id, None)
                connection.capacity.set()

    async def handle(self, control, kind, flags, payload):
        if not control:
            connection = self.streams.get(kind)
            if connection is None:
                # kubectl may race a final FIN against stream cleanup.
                if payload:
                    await self.send(frame(3, struct.pack('!II', kind, 2)))
                return
            if kind == connection.streams.get('data'):
                if connection.eof:
                    if payload:
                        raise ValueError('data received after stream EOF')
                    return
                for offset in range(0, len(payload), 32768):
                    chunk = payload[offset:offset + 32768]
                    while connection.pending + len(chunk) > MAX_PENDING:
                        connection.capacity.clear()
                        try:
                            await asyncio.wait_for(connection.capacity.wait(), 10)
                        except asyncio.TimeoutError:
                            connection.task.cancel()
                            await self.send(frame(3, struct.pack('!II', kind, 5)))
                            return
                        if connection.task.done():
                            return
                    connection.pending += len(chunk)
                    connection.queue.put_nowait(chunk)
                if flags & 1:
                    connection.eof = True
                    connection.queue.put_nowait(None)
            return
        if kind == 1:  # SYN_STREAM
            if len(payload) < 10:
                raise ValueError('truncated SYN_STREAM')
            stream, associated = struct.unpack_from('!II', payload)
            if not stream & 1 or stream <= self.last_stream or stream > 0x7fffffff or associated:
                raise ValueError('invalid stream ID')
            self.last_stream = stream
            headers = self.headers.decode(payload[10:])
            stream_type = headers.get('streamtype')
            request_id = headers.get('requestid')
            port = int(headers.get('port', '0'))
            if stream_type not in ('data', 'error') or request_id is None or not 0 < port < 65536:
                raise ValueError('invalid portforward stream headers')
            connection = self.connections.get(request_id)
            if connection is None:
                if len(self.connections) >= MAX_CONNECTIONS:
                    await self.send(frame(3, struct.pack('!II', stream, 3)))
                    return
                connection = Forward(port)
                self.connections[request_id] = connection
                connection.task = asyncio.create_task(self.forward(request_id, connection))
                self.tasks.add(connection.task)
                def finished(task, identifier=request_id, forward=connection):
                    self.tasks.discard(task)
                    # A task canceled before its first step never enters finally.
                    if self.connections.get(identifier) is forward:
                        self.connections.pop(identifier, None)
                        for sid in forward.streams.values():
                            self.streams.pop(sid, None)
                        forward.capacity.set()
                    if not task.cancelled():
                        task.exception()  # Retrieve failures even after stream removal.
                connection.task.add_done_callback(finished)
            if port != connection.port or stream_type in connection.streams:
                raise ValueError('mismatched portforward stream pair')
            connection.streams[stream_type] = stream
            self.streams[stream] = connection
            # Compression state must advance in the same order as wire writes.
            async with self.send_lock:
                await asyncio.wait_for(self.ws.send_bytes(frame(2, struct.pack('!I', stream) + self.headers.encode({}))), 10)
            if len(connection.streams) == 2:
                connection.ready.set()
            if flags & 1 and stream_type == 'data':
                connection.eof = True
                connection.queue.put_nowait(None)
        elif kind == 3:  # RST_STREAM
            if len(payload) != 8:
                raise ValueError('invalid reset frame')
            stream = struct.unpack_from('!I', payload)[0]
            connection = self.streams.get(stream)
            if connection:
                connection.task.cancel()
        elif kind == 6:  # PING: clients use odd IDs
            if len(payload) != 4:
                raise ValueError('invalid ping frame')
            if struct.unpack('!I', payload)[0] & 1:
                await self.send(frame(6, payload))
        elif kind == 7:  # GOAWAY
            return False
        elif kind not in (4, 9):  # SETTINGS/WINDOW_UPDATE: not used by kubectl's spdystream
            raise ValueError('unsupported SPDY control frame')
        return True

    async def run(self):
        pending = bytearray()
        try:
            while True:
                message = await self.ws.receive()
                if message['type'] == 'websocket.disconnect':
                    break
                data = message.get('bytes')
                if data is None:
                    raise ValueError('portforward requires binary WebSocket messages')
                pending.extend(data)
                while len(pending) >= 8:
                    first, second = struct.unpack_from('!II', pending)
                    length = second & 0xffffff
                    if length > MAX_FRAME:
                        raise ValueError('portforward frame too large')
                    if len(pending) < length + 8:
                        break
                    payload = bytes(pending[8:length + 8])
                    del pending[:length + 8]
                    control = bool(first & 0x80000000)
                    if control and (first >> 16) != 0x8003:
                        raise ValueError('unsupported SPDY version')
                    kind = first & 0xffff if control else first & 0x7fffffff
                    if await self.handle(control, kind, second >> 24, payload) is False:
                        return
        except (WebSocketDisconnect, OSError, asyncio.TimeoutError):
            pass
        except (ValueError, zlib.error):
            await self.ws.close(code=1002, reason='invalid portforward stream')
        finally:
            self.closing = True
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
