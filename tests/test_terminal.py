import asyncio
import json
from urllib.parse import urlencode

from starlette.testclient import TestClient

from pocketkube.api import create_app
from pocketkube.runtime.terminal import spawn_terminal


def test_terminal_ctrl_c_resize_and_exit():
    async def run():
        proc = await spawn_terminal(['/bin/sh'])
        async def until(marker):
            data = b''
            while marker not in data:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), 3)
                assert chunk, data
                data += chunk
            return data
        try:
            proc.resize(101, 37)
            proc.stdin.write(b"stty size; sleep 30\n")
            await proc.stdin.drain()
            await until(b'37 101\r\n')
            # Let the shell enter its foreground job before delivering VINTR.
            await asyncio.sleep(.1)
            proc.stdin.write(b'\x03')
            await proc.stdin.drain()
            proc.stdin.write(b"printf '\\nAFTER_INTERRUPT\\n'\n")
            await proc.stdin.drain()
            await until(b'\r\nAFTER_INTERRUPT\r\n')
            proc.stdin.write(b'exit\n')
            await proc.stdin.drain()
            assert await asyncio.wait_for(proc.wait(), 3) == 0
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            proc.close()
    asyncio.run(run())


def client_with_shell():
    app = create_app('proot')
    processes = []
    async def stream(ns, name, pod, command, tty=False):
        if tty:
            proc = await spawn_terminal(command)
        else:
            proc = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        processes.append(proc)
        return proc
    app.state.pk_runtime.exec_stream = stream
    return app, processes


def seed(client, app):
    client.portal.call(app.state.pk_state.put, 'pods', 'default', 'shell',
                       {'metadata': {'name': 'shell'}, 'spec': {'containers': [{'image': 'test'}]}})


def endpoint(command, **options):
    pairs = [('command', c) for c in command]
    pairs.extend((k, str(v).lower()) for k, v in options.items())
    return '/api/v1/namespaces/default/pods/shell/exec?' + urlencode(pairs)


def test_websocket_tty_resize_and_exit():
    app, processes = client_with_shell()
    with TestClient(app) as client:
        seed(client, app)
        with client.websocket_connect(endpoint(['/bin/sh'], tty=True, stdin=True, stdout=True),
                                      subprotocols=['v5.channel.k8s.io']) as ws:
            ws.send_bytes(b'\x04' + json.dumps({'Width': 99, 'Height': 35}).encode())
            ws.send_bytes(b'\x00stty size; test -t 0 && echo terminal-ok; exit\n')
            output = b''
            while True:
                data = ws.receive_bytes()
                if data[0] == 3:
                    assert json.loads(data[1:])['status'] == 'Success'
                    break
                assert data[0] == 1
                output += data[1:]
            assert b'35 99\r\n' in output
            assert b'\r\nterminal-ok\r\n' in output
        assert processes[0].returncode == 0


def test_websocket_stdin_eof_and_nonzero_status():
    app, _ = client_with_shell()
    with TestClient(app) as client:
        seed(client, app)
        with client.websocket_connect(endpoint(['/bin/sh', '-c', 'cat; echo error >&2; exit 7'],
                                               stdin=True, stdout=True, stderr=True),
                                      subprotocols=['v5.channel.k8s.io']) as ws:
            ws.send_bytes(b'\x00hello\n')
            ws.send_bytes(b'\xff\x00')
            output = {1: b'', 2: b''}
            while True:
                data = ws.receive_bytes()
                if data[0] == 3:
                    assert json.loads(data[1:])['details']['causes'][0]['message'] == '7'
                    break
                output[data[0]] += data[1:]
            assert output[1] == b'hello\n'
            assert output[2] == b'error\n'


def test_websocket_disconnect_terminates_exec():
    app, processes = client_with_shell()
    with TestClient(app) as client:
        seed(client, app)
        with client.websocket_connect(endpoint(['/bin/sh', '-c', 'echo ready; exec sleep 60'],
                                               tty=True, stdin=True, stdout=True),
                                      subprotocols=['v5.channel.k8s.io']) as ws:
            assert b'ready' in ws.receive_bytes()
        async def stopped():
            for _ in range(50):
                if processes[0].returncode is not None:
                    return
                await asyncio.sleep(.05)
            raise AssertionError('exec survived disconnect')
        client.portal.call(stopped)


def test_terminal_ctrl_d_exits_shell():
    async def run():
        proc = await spawn_terminal(['/bin/sh'])
        try:
            await asyncio.wait_for(proc.stdout.read(4096), 3)
            proc.stdin.write(b'\x04')
            await proc.stdin.drain()
            assert await asyncio.wait_for(proc.wait(), 3) == 0
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            proc.close()
    asyncio.run(run())


def test_exec_without_stdin_and_disabled_stderr_does_not_block():
    app, _ = client_with_shell()
    with TestClient(app) as client:
        seed(client, app)
        with client.websocket_connect(endpoint(
                ['/bin/sh', '-c', 'cat; head -c 200000 /dev/zero >&2; echo done'],
                stdout=True), subprotocols=['v4.channel.k8s.io']) as ws:
            output = b''
            while True:
                data = ws.receive_bytes()
                if data[0] == 3:
                    assert json.loads(data[1:])['status'] == 'Success'
                    break
                assert data[0] == 1
                output += data[1:]
            assert output == b'done\n'


def test_exec_spawn_error_is_reported():
    app, _ = client_with_shell()
    with TestClient(app) as client:
        seed(client, app)
        with client.websocket_connect(endpoint(['/no/such/command'], stdout=True),
                                      subprotocols=['v5.channel.k8s.io']) as ws:
            data = ws.receive_bytes()
            assert data[0] == 3
            assert json.loads(data[1:])['status'] == 'Failure'
            assert '/no/such/command' in json.loads(data[1:])['message']
