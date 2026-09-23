import asyncio
import json
import sys

import pytest
from starlette.testclient import TestClient

from pocketkube.api import create_app
from pocketkube.runtime.logs import LogBuffer, LogOptions, limit_stream
from pocketkube.runtime.raw_proot import RawProotRuntime


async def collect(stream):
    return b''.join([chunk async for chunk in stream])


def test_log_tail_timestamps_since_and_partial_lines():
    async def run():
        logs = LogBuffer()
        logs.append(b'old\nfir', now=1)
        logs.append(b'st\nlast', now=2)
        logs.close()
        assert await collect(logs.stream(LogOptions())) == b'old\nfirst\nlast'
        assert await collect(logs.stream(LogOptions(tail=2))) == b'first\nlast'
        assert await collect(logs.stream(LogOptions(tail=0))) == b''
        assert await collect(logs.stream(LogOptions(since=2))) == b'last'
        output = await collect(logs.stream(LogOptions(timestamps=True)))
        assert output == (b'1970-01-01T00:00:01.000000Z old\n'
                          b'1970-01-01T00:00:01.000000Z first\n'
                          b'1970-01-01T00:00:02.000000Z last')
        assert await collect(limit_stream(logs.stream(LogOptions()), 5)) == b'old\nf'
    asyncio.run(run())


def test_follow_no_gaps_or_duplicates_and_multiple_readers():
    async def run():
        logs = LogBuffer()
        logs.append(b'history\n')
        first = logs.stream(LogOptions(follow=True))
        second = logs.stream(LogOptions(follow=True, tail=0))
        assert await first.__anext__() == b'history\n'
        pending = asyncio.create_task(second.__anext__())
        await asyncio.sleep(0)
        logs.append(b'new\n')
        assert await asyncio.wait_for(pending, 1) == b'new\n'
        assert await first.__anext__() == b'new\n'
        logs.close()
        assert await collect(first) == b''
        assert await collect(second) == b''
    asyncio.run(run())


def test_history_is_bounded_and_slow_followers_resume():
    async def run():
        logs = LogBuffer(max_bytes=32, max_records=4)
        logs.append(b'first\n')
        stream = logs.stream(LogOptions(follow=True))
        assert await stream.__anext__() == b'first\n'
        for _ in range(100):
            logs.append(b'0123456789\n')
        logs.append(b'last\n')
        assert logs.size <= 32
        assert len(logs.records) <= 4
        logs.close()
        output = await collect(stream)
        assert output.endswith(b'last\n')
        assert b'first' not in output
        assert len(output) <= 32
    asyncio.run(run())


@pytest.mark.parametrize('params', [
    {'follow': 'invalid'}, {'tailLines': '-2'}, {'tailLines': '1.2'},
    {'limitBytes': '0'}, {'sinceSeconds': '-1'}, {'sinceTime': 'yesterday'},
    {'sinceSeconds': '1', 'sinceTime': '2026-01-01T00:00:00Z'},
    {'previous': 'true'}, {'stream': 'Stderr'},
])
def test_invalid_options(params):
    with pytest.raises(ValueError):
        LogOptions.parse(params)


def test_options_parse_relative_and_absolute_time(monkeypatch):
    monkeypatch.setattr('pocketkube.runtime.logs.time.time', lambda: 100)
    assert LogOptions.parse({'sinceSeconds': '10'}).since == 90
    assert LogOptions.parse({'sinceTime': '1970-01-01T01:00:02+01:00'}).since == 2
    assert LogOptions.parse({'tailLines': '-1'}).tail == -1


PATH = '/api/v1/namespaces/default/pods/test/log'
POD = {'metadata': {'name': 'test'}, 'spec': {'containers': [{'name': 'main', 'image': 'alpine'}]}}


def seed(client, app):
    client.portal.call(app.state.pk_state.put, 'pods', 'default', 'test', POD)


def test_log_api_discovery_errors_and_filtered_output():
    app = create_app('proot')
    logs = LogBuffer()
    logs.append(b'one\ntwo\nthree\n', now=2)
    logs.close()
    app.state.pk_runtime._logs[('default', 'test')] = logs
    with TestClient(app) as client:
        assert client.get(PATH).status_code == 404
        seed(client, app)
        discovery = client.get('/api/v1').json()
        assert any(r['name'] == 'pods/log' and r['verbs'] == ['get'] for r in discovery['resources'])
        response = client.get(PATH, params={'container': 'main', 'tailLines': 2, 'limitBytes': 5})
        assert response.status_code == 200
        assert response.content == b'two\nt'
        assert response.headers['content-type'].startswith('text/plain')
        assert client.get(PATH, params={'container': 'wrong'}).status_code == 400
        assert client.get(PATH, params={'previous': 'true'}).status_code == 400
        assert client.get(PATH, params={'tailLines': 'bad'}).status_code == 400
        app.state.pk_runtime._logs.clear()
        assert client.get(PATH).status_code == 400


def test_follow_http_stream_ends_when_output_closes():
    app = create_app('proot')
    with TestClient(app) as client:
        seed(client, app)
        async def produce():
            logs = LogBuffer()
            app.state.pk_runtime._logs[('default', 'test')] = logs
            logs.append(b'first\n')
            async def later():
                await asyncio.sleep(.05)
                logs.append(b'second\n')
                logs.close()
            asyncio.create_task(later())
        client.portal.call(produce)
        response = client.get(PATH, params={'follow': 'true'})
        assert response.content == b'first\nsecond\n'


def test_failed_start_logs_retained_until_deletion(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    (root / 'bin').mkdir(parents=True)
    (root / 'bin/sh').touch()
    runtime = RawProotRuntime(rootfs=root)
    runtime.images.directory = tmp_path / 'images'
    monkeypatch.setattr(runtime, '_pod_command', lambda *args: [
        sys.executable, '-c', "import sys; print('out', flush=True); print('err', file=sys.stderr); sys.exit(1)"])
    async def run():
        with pytest.raises(RuntimeError):
            await runtime.start_pod('default', 'test', POD)
        stream = await runtime.logs('default', 'test', POD, LogOptions(follow=True))
        assert await asyncio.wait_for(collect(stream), 1) == b'out\nerr\n'
        await runtime.stop_pod('default', 'test', POD)
        with pytest.raises(RuntimeError, match='not available'):
            await runtime.logs('default', 'test', POD, LogOptions())
    asyncio.run(run())


def test_docker_logs_options_and_cleanup(monkeypatch):
    from pocketkube.runtime.docker import DockerRuntime, _name
    runtime = DockerRuntime()
    calls = []
    async def inspect(*args):
        calls.append(args)
        return 0, b'id', b''
    monkeypatch.setattr(runtime, '_call', inspect)
    class Process:
        returncode = None
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b'hello\n')
        def terminate(self):
            self.returncode = -15
        async def wait(self):
            return self.returncode
    processes = []
    async def spawn(*args, **kwargs):
        calls.append(args)
        proc = Process()
        processes.append(proc)
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    async def run():
        stream = await runtime.logs('default', 'test', POD,
                                    LogOptions(follow=True, timestamps=True, tail=2, since=1))
        assert await collect(limit_stream(stream, 3)) == b'hel'
        assert processes[0].returncode == -15
        assert calls[1] == ('docker', 'logs', '--tail', '2', '--follow', '--timestamps',
                            '--since', '1970-01-01T00:00:01.000000Z', _name('default', 'test'))
    asyncio.run(run())


@pytest.mark.parametrize('asgi_version', ['2.3', '2.4'])
def test_http_disconnect_closes_only_the_follower(asgi_version):
    from starlette.requests import ClientDisconnect
    async def run():
        app = create_app('proot')
        await app.state.pk_state.put('pods', 'default', 'test', POD)
        logs = LogBuffer()
        logs.append(b'first\n')
        closed = asyncio.Event()
        disconnected = asyncio.Event()
        async def get_logs(*args):
            async def stream():
                try:
                    async for chunk in logs.stream(LogOptions(follow=True)):
                        yield chunk
                finally:
                    closed.set()
            return stream()
        app.state.pk_runtime.logs = get_logs
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': asgi_version},
                 'http_version': '1.1', 'method': 'GET', 'scheme': 'http', 'path': PATH,
                 'raw_path': PATH.encode(), 'query_string': b'follow=true', 'root_path': '',
                 'headers': [], 'server': ('test', 80), 'client': ('test', 123)}
        async def receive():
            await disconnected.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            if message['type'] == 'http.response.body' and message.get('body'):
                disconnected.set()
                if asgi_version == '2.4':
                    raise OSError('disconnected')
        try:
            await asyncio.wait_for(app(scope, receive, send), 2)
        except ClientDisconnect:
            pass
        assert closed.is_set()
        assert not logs.closed
        logs.append(b'still capturing\n')
        assert (await collect(logs.stream(LogOptions()))).endswith(b'still capturing\n')
    asyncio.run(run())
