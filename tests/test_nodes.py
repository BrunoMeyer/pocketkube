from starlette.testclient import TestClient
import pytest

from pocketkube.api import create_app
from pocketkube.nodes import local_node


def test_nodes_discovery_list_get_and_errors(monkeypatch):
    monkeypatch.setenv('POCKETKUBE_NODE_NAME', 'phone')
    monkeypatch.setenv('POCKETKUBE_NODE_IP', '192.168.1.5')
    monkeypatch.setattr('platform.machine', lambda: 'armv7l')
    with TestClient(create_app('proot')) as client:
        resource = next(r for r in client.get('/api/v1').json()['resources'] if r['name'] == 'nodes')
        assert resource['namespaced'] is False
        assert resource['verbs'] == ['get', 'list', 'patch']
        listing = client.get('/api/v1/nodes').json()
        assert listing['kind'] == 'NodeList'
        node = listing['items'][0]
        assert node['metadata']['name'] == 'phone'
        assert node == client.get('/api/v1/nodes/phone').json()
        assert 'namespace' not in node['metadata']
        assert node['status']['nodeInfo']['architecture'] == 'arm'
        assert node['status']['addresses'][-1]['address'] == '192.168.1.5'
        response = client.patch(
            '/api/v1/nodes/phone',
            content='{"metadata":{"labels":{"mentored":"ready"}}}',
            headers={'content-type': 'application/strategic-merge-patch+json'},
        )
        assert response.status_code == 200
        assert response.json()['metadata']['labels']['mentored'] == 'ready'
        assert client.get('/api/v1/nodes/phone').json()['metadata']['labels']['mentored'] == 'ready'
        assert client.get('/api/v1/nodes/missing').status_code == 404
        assert client.delete('/api/v1/nodes/phone').status_code == 405
        assert client.get('/api/v1/nodes?fieldSelector=metadata.name=missing').json()['items'] == []
        assert client.get('/api/v1/nodes?watch=true').status_code == 400


def test_pod_assignment_and_describe_filter(monkeypatch):
    monkeypatch.setenv('POCKETKUBE_NODE_NAME', 'phone')
    app = create_app('proot')
    async def start(*args):
        pass
    app.state.pk_runtime.start_pod = start
    with TestClient(app) as client:
        body = {'kind': 'Pod', 'metadata': {'name': 'demo'}, 'spec': {'containers': [{'name': 'main', 'image': 'alpine'}]}}
        response = client.post('/api/v1/namespaces/default/pods', json=body)
        assert response.status_code == 201
        assert response.json()['spec']['nodeName'] == 'phone'
        response = client.get('/api/v1/pods', params={'fieldSelector': 'spec.nodeName=phone,status.phase!=Succeeded,status.phase!=Failed'})
        assert len(response.json()['items']) == 1
        assert client.get('/api/v1/pods?fieldSelector=spec.nodeName=other').json()['items'] == []
        body['spec']['nodeName'] = 'other'
        assert client.post('/api/v1/namespaces/default/pods', json=body).status_code == 422
        assert client.get('/api/v1/events').json()['kind'] == 'EventList'


def test_host_metadata_fallbacks(monkeypatch):
    monkeypatch.delenv('POCKETKUBE_NODE_NAME', raising=False)
    monkeypatch.delenv('POCKETKUBE_NODE_IP', raising=False)
    monkeypatch.setattr('socket.gethostname', lambda: 'My_Phone')
    monkeypatch.setattr('os.cpu_count', lambda: None)
    def denied(*args):
        raise OSError('not available')
    monkeypatch.setattr('os.sysconf', denied)
    node = local_node('docker')
    assert node['metadata']['name'] == 'my-phone'
    assert node['status']['capacity'] == {}


@pytest.mark.parametrize('name', ['Invalid_Name', '../escape', 'a' * 64])
def test_invalid_configured_name(monkeypatch, name):
    monkeypatch.setenv('POCKETKUBE_NODE_NAME', name)
    with pytest.raises(ValueError):
        local_node('proot')


def test_node_table_has_standard_and_wide_columns(monkeypatch):
    monkeypatch.setenv('POCKETKUBE_NODE_NAME', 'phone')
    with TestClient(create_app('proot')) as client:
        headers = {'Accept': 'application/json;as=Table;v=v1;g=meta.k8s.io,application/json'}
        table = client.get('/api/v1/nodes', headers=headers).json()
        assert table['kind'] == 'Table'
        assert table['rows'][0]['cells'][:3] == ['phone', 'Ready', 'pocketkube']
        assert table['columnDefinitions'][5]['priority'] == 1
        assert len(table['rows'][0]['cells']) == len(table['columnDefinitions'])
        assert client.get('/api/v1/nodes/phone', headers=headers).json()['kind'] == 'Table'
        assert client.get('/api/v1/nodes/phone').json()['kind'] == 'Node'


@pytest.mark.parametrize('restricted', [False, True])
def test_required_node_info_fields(monkeypatch, restricted):
    from pathlib import Path
    values = {
        '/proc/sys/kernel/random/boot_id': 'boot-id\n',
        '/etc/machine-id': '',
        '/var/lib/dbus/machine-id': 'machine-id\n',
        '/sys/class/dmi/id/product_uuid': 'system-uuid\n',
    }
    original = Path.read_text
    def read(path, *args, **kwargs):
        if str(path) in values:
            if restricted:
                raise PermissionError('restricted host')
            return values[str(path)]
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    with TestClient(create_app('proot')) as client:
        node = client.get('/api/v1/nodes').json()['items'][0]
        info = node['status']['nodeInfo']
        required = ['architecture', 'bootID', 'containerRuntimeVersion', 'kernelVersion',
                    'kubeProxyVersion', 'kubeletVersion', 'machineID', 'operatingSystem',
                    'osImage', 'systemUUID']
        assert all(isinstance(info[key], str) for key in required)
        assert info['bootID'] == ('' if restricted else 'boot-id')
        assert info['machineID'] == ('' if restricted else 'machine-id')
        assert info['systemUUID'] == ('' if restricted else 'system-uuid')
        assert info['kubeProxyVersion'] == ''


@pytest.mark.parametrize('restricted', [False, True])
def test_python_client_list_and_read_node(monkeypatch, restricted):
    # Optional client dependency: production PocketKube does not need this SDK.
    kubernetes = pytest.importorskip('kubernetes.client')
    from urllib.parse import urlsplit
    from urllib3.response import HTTPResponse
    if restricted:
        monkeypatch.setattr('pocketkube.nodes._host_identifier', lambda *paths: '')
    with TestClient(create_app('proot')) as client, kubernetes.ApiClient() as api_client:
        def request(method, url, **kwargs):
            response = client.request(method, urlsplit(url).path)
            return HTTPResponse(body=response.content, status=response.status_code,
                                headers=dict(response.headers))
        monkeypatch.setattr(api_client, 'request', request)
        api = kubernetes.CoreV1Api(api_client)
        nodes = api.list_node()
        assert len(nodes.items) == 1
        node = api.read_node(nodes.items[0].metadata.name)
        assert node.status.node_info == nodes.items[0].status.node_info
        assert isinstance(node.status.node_info.boot_id, str)
        assert isinstance(node.status.node_info.machine_id, str)
        assert isinstance(node.status.node_info.system_uuid, str)
        assert node.status.node_info.kube_proxy_version == ''


@pytest.mark.parametrize('content_type', ['application/strategic-merge-patch+json', 'application/merge-patch+json'])
def test_label_patch_merge_removal_conflict_and_dry_run(monkeypatch, content_type):
    monkeypatch.setenv('POCKETKUBE_NODE_NAME', 'localhost')
    path = '/api/v1/nodes/localhost'
    with TestClient(create_app('proot')) as client:
        original = client.get(path).json()
        def patch(labels, **params):
            return client.patch(path, json={'metadata': {'labels': labels}},
                                headers={'content-type': content_type}, params=params)
        assert patch({'mentored': 'ready'}).status_code == 200
        current = client.get(path).json()
        assert current['metadata']['labels']['mentored'] == 'ready'
        assert current['metadata']['labels']['kubernetes.io/hostname'] == 'localhost'
        assert current['metadata']['resourceVersion'] != original['metadata']['resourceVersion']
        assert patch({'mentored': 'busy'}, dryRun='All').json()['metadata']['labels']['mentored'] == 'busy'
        assert client.get(path).json() == current
        conflict = client.patch(path, json={'metadata': {'resourceVersion': original['metadata']['resourceVersion'],
                                                         'labels': {'mentored': 'stale'}}},
                                headers={'content-type': content_type})
        assert conflict.status_code == 409
        assert patch({'mentored': 'busy'}).status_code == 200
        assert patch({'mentored': None}).status_code == 200
        assert 'mentored' not in client.get(path).json()['metadata']['labels']
        before = client.get(path).json()
        assert patch({'good': 'value', 'bad key': 'invalid'}).status_code == 422
        assert client.get(path).json() == before
        assert client.patch(path, json={'spec': {'unschedulable': True}}, headers={'content-type': content_type}).status_code == 422
        assert client.patch(path, json=[]).status_code == 415
