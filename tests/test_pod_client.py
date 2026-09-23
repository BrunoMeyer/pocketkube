import asyncio
from urllib.parse import urlsplit

import pytest
from starlette.testclient import TestClient

from pocketkube.api import create_app


@pytest.mark.parametrize('phase', ['Pending', 'Running', 'Failed'])
def test_python_client_lists_and_reads_pods(monkeypatch, phase):
    kubernetes = pytest.importorskip('kubernetes.client')
    from urllib3.response import HTTPResponse

    app = create_app('proot')
    release = None
    async def start(*args):
        await release.wait()
        if phase == 'Failed':
            raise RuntimeError('test startup failure')
    app.state.pk_runtime.start_pod = start
    with TestClient(app) as http, kubernetes.ApiClient() as api_client:
        async def setup():
            return asyncio.Event()
        release = http.portal.call(setup)
        body = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'demo'},
                'spec': {'containers': [{'name': 'app', 'image': 'nginx:alpine'}]}}
        assert http.post('/api/v1/namespaces/default/pods', json=body).status_code == 201
        if phase != 'Pending':
            async def wait_started():
                release.set()
                for _ in range(100):
                    pod = await app.state.pk_state.get('pods', 'default', 'demo')
                    if pod['status']['phase'] == phase:
                        return
                    await asyncio.sleep(.01)
                raise AssertionError('pod did not reach expected phase')
            http.portal.call(wait_started)
        def request(method, url, **kwargs):
            response = http.request(method, urlsplit(url).path, params=kwargs.get('query_params'))
            return HTTPResponse(body=response.content, status=response.status_code,
                                headers=dict(response.headers))
        monkeypatch.setattr(api_client, 'request', request)
        api = kubernetes.CoreV1Api(api_client)
        pod = api.list_namespaced_pod('default', watch=False).items[0]
        assert pod.status.phase == phase
        assert api.read_namespaced_pod('demo', 'default').status == pod.status
        assert api.list_pod_for_all_namespaces(watch=False).items[0].status == pod.status
        if phase == 'Running':
            status = pod.status.container_statuses[0]
            assert status.name == 'app'
            assert status.image == 'nginx:alpine'
            assert status.image_id == ''
            assert status.ready is True
            assert status.restart_count == 0
            assert status.state.running.started_at is not None
        else:
            assert pod.status.container_statuses == []
        http.portal.call(release.set)


def test_running_status_includes_required_fields_without_sdk():
    from pocketkube.controllers import Controllers
    from pocketkube.state import MemoryState
    class Runtime:
        async def start_pod(self, *args):
            pass
    async def run():
        state = MemoryState()
        pod = {'spec': {'containers': [{'name': 'web', 'image': 'nginx:alpine'}]}, 'status': {}}
        await state.put('pods', 'default', 'web', pod)
        await Controllers(state, Runtime())._start_pod('default', 'web')
        status = (await state.get('pods', 'default', 'web'))['status']['containerStatuses'][0]
        assert status['image'] == 'nginx:alpine'
        assert isinstance(status['imageID'], str)
        assert all(key in status for key in ('name', 'ready', 'restartCount', 'image', 'imageID'))
    asyncio.run(run())
