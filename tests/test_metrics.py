import asyncio
import time

import pytest
from starlette.testclient import TestClient

from pocketkube.api import create_app
from pocketkube.metrics import HostMetrics, read_host


def test_proc_counters_exclude_guest_and_wait(tmp_path, monkeypatch):
    (tmp_path / 'stat').write_text('cpu 100 20 30 500 50 10 5 2 80 10\n')
    (tmp_path / 'meminfo').write_text('MemTotal: 1000 kB\nMemFree: 200 kB\nInactive(file): 100 kB\n')
    monkeypatch.setattr('os.sysconf', lambda name: 100)
    busy, memory, _ = read_host(tmp_path)
    assert busy == 1.67
    assert memory == 700 * 1024


def test_sample_rate_window_and_concurrent_cache(monkeypatch):
    now = time.monotonic()
    samples = iter([(10, 1000, now - .25), (10.5, 2000, now)])
    monkeypatch.setattr('pocketkube.metrics.read_host', lambda: next(samples))
    async def run():
        sampler = HostMetrics()
        first, second = await asyncio.gather(sampler.sample(), sampler.sample())
        assert first == second
        assert first['usage'] == {'cpu': '2000000000n', 'memory': '2000'}
        assert first['window'] == '0.250000000s'
    asyncio.run(run())


def test_counter_failure_is_not_fake_zero(monkeypatch):
    def denied():
        raise PermissionError('proc restricted')
    monkeypatch.setattr('pocketkube.metrics.read_host', denied)
    async def run():
        with pytest.raises(RuntimeError, match='proc restricted'):
            await HostMetrics().sample()
    asyncio.run(run())


def test_metrics_discovery_and_routes(monkeypatch):
    monkeypatch.setenv('POCKETKUBE_NODE_NAME', 'phone')
    app = create_app('proot')
    async def sample():
        return {'timestamp': '2026-09-16T00:00:00Z', 'window': '1s',
                'usage': {'cpu': '500000000n', 'memory': '1024'}}
    app.state.pk_metrics.sample = sample
    with TestClient(app) as client:
        assert any(g['name'] == 'metrics.k8s.io' for g in client.get('/apis').json()['groups'])
        assert client.get('/apis/metrics.k8s.io').json()['kind'] == 'APIGroup'
        for version in ('v1', 'v1beta1'):
            base = '/apis/metrics.k8s.io/' + version
            discovery = client.get(base).json()
            assert discovery['resources'][0]['namespaced'] is False
            listing = client.get(base + '/nodes').json()
            assert listing['kind'] == 'NodeMetricsList'
            assert listing['apiVersion'] == 'metrics.k8s.io/' + version
            assert listing['items'][0] == client.get(base + '/nodes/phone').json()
            assert listing['items'][0]['usage']['cpu'] == '500000000n'
            assert client.get(base + '/nodes/missing').status_code == 404
            assert client.get(base + '/nodes?fieldSelector=metadata.name=missing').json()['items'] == []
        assert client.get('/apis/metrics.k8s.io/v2/nodes').status_code == 404
        node = client.get('/api/v1/nodes/phone').json()
        assert node['status']['allocatable'] == node['status']['capacity']
        async def unavailable():
            raise RuntimeError('host metrics unavailable')
        app.state.pk_metrics.sample = unavailable
        response = client.get('/apis/metrics.k8s.io/v1beta1/nodes')
        assert response.status_code == 503
        assert response.json()['reason'] == 'ServiceUnavailable'
