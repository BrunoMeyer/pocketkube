import asyncio
import struct
from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pocketkube.api import create_app
from pocketkube.portforward import Headers, MAX_FRAME, PROTOCOLS, frame

PATH = '/api/v1/namespaces/default/pods/echo/portforward'


def syn(codec, stream, request, port, kind, flags=0):
    headers = {'streamtype': kind, 'port': str(port), 'requestid': str(request)}
    return frame(1, struct.pack('!IIH', stream, 0, 0) + codec.encode(headers), flags)


def unpack(data):
    first, second = struct.unpack_from('!II', data)
    assert len(data) == 8 + (second & 0xffffff)
    return bool(first & 0x80000000), first & 0xffff if first & 0x80000000 else first, second >> 24, data[8:]


@contextmanager
def endpoint():
    app = create_app('proot')
    sockets = set()
    async def echo(reader, writer):
        sockets.add(writer)
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            sockets.discard(writer)
    async def connect(ns, name, pod, port):
        return await asyncio.open_connection('127.0.0.1', port)
    app.state.pk_runtime.open_port = connect
    with TestClient(app) as client:
        async def setup():
            await app.state.pk_state.put('pods', 'default', 'echo', {
                'metadata': {'name': 'echo', 'namespace': 'default', 'uid': 'test'},
                'spec': {'containers': [{'name': 'echo', 'image': 'test'}]},
                'status': {'phase': 'Running'},
            })
            return await asyncio.start_server(echo, '127.0.0.1', 0)
        server = client.portal.call(setup)
        try:
            yield app, client, server.sockets[0].getsockname()[1], sockets
        finally:
            async def cleanup():
                server.close()
                await server.wait_closed()
                for writer in list(sockets):
                    writer.close()
                    await writer.wait_closed()
            client.portal.call(cleanup)


def open_pair(ws, codec, port, request=0, first=1):
    ws.send_bytes(syn(codec, first, request, port, 'error', flags=1))
    assert unpack(ws.receive_bytes())[:2] == (True, 2)
    ws.send_bytes(syn(codec, first + 2, request, port, 'data'))
    assert unpack(ws.receive_bytes())[:2] == (True, 2)


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_forward_fragmented_frames_half_close_and_reuse(protocol):
    with endpoint() as (_, client, port, _):
        with client.websocket_connect(PATH, subprotocols=[protocol]) as ws:
            assert ws.accepted_subprotocol == protocol
            codec = Headers()
            for request, first in [(0, 1), (1, 5)]:
                open_pair(ws, codec, port, request, first)
                payload = b'\x00\xffbinary\n' * 5000
                packet = frame(0, payload, flags=1, stream=first + 2)
                ws.send_bytes(packet[:5])
                ws.send_bytes(packet[5:100])
                ws.send_bytes(packet[100:])
                output = b''
                ended = set()
                while len(ended) < 2:
                    control, stream, flags, data = unpack(ws.receive_bytes())
                    assert not control
                    if stream == first + 2:
                        output += data
                    else:
                        assert not data
                    if flags & 1:
                        ended.add(stream)
                assert output == payload
            ws.send_bytes(frame(6, struct.pack('!I', 1)))
            assert unpack(ws.receive_bytes()) == (True, 6, 0, struct.pack('!I', 1))


def test_connection_refusal_uses_error_stream():
    with endpoint() as (app, client, port, _):
        async def refused(*args):
            raise ConnectionRefusedError('test refusal')
        app.state.pk_runtime.open_port = refused
        with client.websocket_connect(PATH, subprotocols=[PROTOCOLS[0]]) as ws:
            open_pair(ws, Headers(), port)
            errors = b''
            ended = set()
            while len(ended) < 2:
                control, stream, flags, data = unpack(ws.receive_bytes())
                if stream == 1:
                    errors += data
                if flags & 1:
                    ended.add(stream)
            assert b'test refusal' in errors
            assert str(port).encode() in errors


@pytest.mark.parametrize('packet', [
    struct.pack('!II', 1, MAX_FRAME + 1),
    frame(1, b'short'), frame(6, b'x'),
])
def test_bad_frames_close_protocol(packet):
    with endpoint() as (_, client, _, _):
        with client.websocket_connect(PATH, subprotocols=[PROTOCOLS[0]]) as ws:
            ws.send_bytes(packet)
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_bytes()
            assert error.value.code == 1002


def test_discovery_and_http_upgrade_errors():
    with endpoint() as (_, client, _, _):
        resources = client.get('/api/v1').json()['resources']
        assert any(r['name'] == 'pods/portforward' for r in resources)
        assert client.post(PATH).status_code == 426
        assert client.get(PATH).status_code == 426
        with pytest.raises(Exception) as error:
            with client.websocket_connect(PATH, subprotocols=['v5.channel.k8s.io']):
                pass
        assert getattr(error.value, 'status_code', None) == 400


def test_disconnect_and_pod_deletion_close_tcp():
    with endpoint() as (app, client, port, sockets):
        async def closed():
            for _ in range(100):
                if not sockets:
                    return
                await asyncio.sleep(.02)
            raise AssertionError('forwarded TCP connection survived disconnect')
        for delete in (False, True):
            with client.websocket_connect(PATH, subprotocols=[PROTOCOLS[0]]) as ws:
                open_pair(ws, Headers(), port)
                ws.send_bytes(frame(0, b'connected', stream=3))
                assert unpack(ws.receive_bytes())[3] == b'connected'
                if delete:
                    client.portal.call(app.state.pk_state.delete, 'pods', 'default', 'echo')
                    with pytest.raises(WebSocketDisconnect):
                        ws.receive_bytes()
            client.portal.call(closed)


def test_raw_runtime_only_connects_for_running_pod(monkeypatch):
    from pocketkube.runtime.raw_proot import RawProotRuntime
    runtime = RawProotRuntime()
    calls = []
    async def connect(host, port):
        calls.append((host, port))
        return 'reader', 'writer'
    monkeypatch.setattr(asyncio, 'open_connection', connect)
    async def run():
        with pytest.raises(RuntimeError, match='not running'):
            await runtime.open_port('default', 'echo', {}, 8080)
        class Process:
            returncode = None
        runtime._processes[('default', 'echo')] = Process()
        assert await runtime.open_port('default', 'echo', {}, 8080) == ('reader', 'writer')
        assert calls == [('127.0.0.1', 8080)]
    asyncio.run(run())


def test_reset_cleans_up_stream_and_allows_another_connection():
    with endpoint() as (_, client, port, sockets):
        with client.websocket_connect(PATH, subprotocols=[PROTOCOLS[0]]) as ws:
            codec = Headers()
            open_pair(ws, codec, port)
            ws.send_bytes(frame(0, b'ready', stream=3))
            assert unpack(ws.receive_bytes())[3] == b'ready'
            ws.send_bytes(frame(3, struct.pack('!II', 3, 5)))
            ended = set()
            while len(ended) < 2:
                _, stream, flags, _ = unpack(ws.receive_bytes())
                if flags & 1:
                    ended.add(stream)
            open_pair(ws, codec, port, request=1, first=5)
            ws.send_bytes(frame(0, b'again', stream=7))
            assert unpack(ws.receive_bytes())[3] == b'again'


def test_docker_connects_to_container_address(monkeypatch):
    import json
    from pocketkube.runtime.docker import DockerRuntime
    runtime = DockerRuntime()
    async def inspect(*args):
        return 0, json.dumps([{'State': {'Running': True}, 'HostConfig': {'NetworkMode': 'bridge'},
                               'NetworkSettings': {'Networks': {'bridge': {'IPAddress': '172.17.0.2'}}}}]).encode(), b''
    async def connect(host, port):
        assert (host, port) == ('172.17.0.2', 8080)
        return 'reader', 'writer'
    monkeypatch.setattr(runtime, '_call', inspect)
    monkeypatch.setattr(asyncio, 'open_connection', connect)
    assert asyncio.run(runtime.open_port('default', 'echo', {}, 8080)) == ('reader', 'writer')
