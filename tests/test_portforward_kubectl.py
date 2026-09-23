import asyncio, os, re, socket, tempfile, shutil
import pytest
from pathlib import Path
import uvicorn
from pocketkube.api import create_app
from pocketkube.cli import kubeconfig

async def exercise_kubectl():
    writers=set()
    async def echo(reader,writer):
        writers.add(writer)
        try:
            while True:
                data=await reader.read(65536)
                if not data: break
                writer.write(data); await writer.drain()
        finally:
            writer.close();await writer.wait_closed();writers.discard(writer)
    echo_server=await asyncio.start_server(echo,'127.0.0.1',0)
    remote=echo_server.sockets[0].getsockname()[1]
    app=create_app('proot')
    async def connect(ns,name,pod,port):
        return await asyncio.open_connection('127.0.0.1',port)
    app.state.pk_runtime.open_port=connect
    await app.state.pk_state.put('pods','default','echo',{'apiVersion':'v1','kind':'Pod','metadata':{'name':'echo','namespace':'default','uid':'smoke'},'spec':{'containers':[{'name':'echo','image':'test'}]},'status':{'phase':'Running'}})
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',ws='websockets'))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    kubectl=None
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'config';path.write_text(kubeconfig('http://127.0.0.1:'+str(sock.getsockname()[1])))
        try:
            while not server.started: await asyncio.sleep(.01)
            env=os.environ.copy();env['KUBECTL_PORT_FORWARD_WEBSOCKETS']='true'
            kubectl=await asyncio.create_subprocess_exec('kubectl','--kubeconfig',str(path),'port-forward','pod/echo',':'+str(remote),'--address=127.0.0.1',env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            line=await asyncio.wait_for(kubectl.stdout.readline(),10)
            if not line:
                raise RuntimeError('kubectl exited before readiness')
            print(line.decode().strip(),flush=True)
            local=int(re.search(rb'127.0.0.1:(\d+)',line)[1])
            async def check(i):
                reader,writer=await asyncio.open_connection('127.0.0.1',local)
                data=(bytes([i])*1000000)+b'end'
                async def write():
                    writer.write(data);await writer.drain()
                sending=asyncio.create_task(write())
                try:
                    result=await asyncio.wait_for(reader.readexactly(len(data)),15)
                    assert result==data
                    await sending
                finally:
                    writer.close();await writer.wait_closed()
            await asyncio.gather(*(check(i) for i in range(4)))
            await check(5)
            print('Concurrent 1 MB round trips and reconnect passed',flush=True)
            kubectl.terminate();await asyncio.wait_for(kubectl.wait(),5)
            await asyncio.sleep(.2)
            assert not writers, len(writers)
            print('Disconnect cleanup passed',flush=True)
        finally:
            if kubectl and kubectl.returncode is None:
                kubectl.kill();await kubectl.wait()

            server.should_exit=True
            await task
            echo_server.close();await echo_server.wait_closed()
            sock.close()
@pytest.mark.skipif(shutil.which('kubectl') is None, reason='kubectl is not installed')
def test_live_kubectl_concurrent_tcp_forwarding():
    asyncio.run(exercise_kubectl())
