import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.error import HTTPError

import pytest

from pocketkube.runtime.images import ImageStore, apply_layer, host_platform, reference
from pocketkube.runtime.raw_proot import RawProotRuntime


def layer(tmp_path, entries):
    archive = tmp_path / 'layer.tgz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name, value, kind in entries:
            entry = tarfile.TarInfo(name)
            if kind == 'symlink':
                entry.type = tarfile.SYMTYPE
                entry.linkname = value
                tar.addfile(entry)
            else:
                data = value.encode()
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
    return archive


@pytest.mark.parametrize('value,expected', [
    ('nginx:alpine', ('docker.io', 'library/nginx', 'alpine')),
    ('org/app', ('docker.io', 'org/app', 'latest')),
    ('ghcr.io/org/app:v1', ('ghcr.io', 'org/app', 'v1')),
    ('docker.io/alpine@sha256:' + 'a' * 64, ('docker.io', 'library/alpine', 'sha256:' + 'a' * 64)),
])
def test_references(value, expected):
    assert reference(value) == expected


@pytest.mark.parametrize('value', ['../escape', 'ghcr.io/../x', 'https://ghcr.io/x', 'x@sha256:bad'])
def test_invalid_reference(value):
    with pytest.raises(RuntimeError):
        reference(value)


def test_armv7(monkeypatch):
    monkeypatch.setattr('platform.machine', lambda: 'armv7l')
    assert host_platform() == 'linux/arm/v7'


def test_whiteouts_and_guest_symlinks(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    apply_layer(root, layer(tmp_path, [('dir/old', 'old', ''), ('gone', 'old', '')]))
    apply_layer(root, layer(tmp_path, [('dir/new', 'new', ''), ('dir/.wh..wh..opq', '', ''),
                                     ('.wh.gone', '', ''), ('link', '/dir', 'symlink'), ('link/other', 'ok', '')]))
    assert not (root / 'gone').exists()
    assert not (root / 'dir/old').exists()
    assert (root / 'dir/new').read_text() == 'new'
    assert (root / 'dir/other').read_text() == 'ok'


def test_absolute_layer_paths_are_guest_root_relative(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    archive = tmp_path / 'absolute.tgz'
    with tarfile.open(archive, 'w:gz') as tar:
        entry = tarfile.TarInfo('/var/lib/chisel')
        data = b'config'
        entry.size = len(data)
        tar.addfile(entry, io.BytesIO(data))
        entry = tarfile.TarInfo('/etc/chisel')
        entry.type = tarfile.LNKTYPE
        entry.linkname = '/var/lib/chisel'
        tar.addfile(entry)
    apply_layer(root, archive)
    assert (root / 'var/lib/chisel').read_bytes() == b'config'
    assert (root / 'etc/chisel').read_bytes() == b'config'


def test_escape_rejected(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    with pytest.raises(RuntimeError, match='unsafe'):
        apply_layer(root, layer(tmp_path, [('../outside', 'bad', '')]))
    with pytest.raises(RuntimeError, match='escapes'):
        apply_layer(root, layer(tmp_path, [('link', '../../', 'symlink'), ('link/outside', 'bad', '')]))
    assert not (tmp_path / 'outside').exists()


@pytest.mark.parametrize('container,expected', [
    ({}, ['entry', 'default']),
    ({'command': ['custom']}, ['custom', 'default']),
    ({'args': ['arg']}, ['entry', 'arg']),
    ({'command': ['custom'], 'args': ['arg']}, ['custom', 'arg']),
])
def test_command_overrides(container, expected):
    assert RawProotRuntime._image_command(container, {'Entrypoint': ['entry'], 'Cmd': ['default']}) == expected


def registry_fixture(tmp_path, monkeypatch, registry='docker.io'):
    store = ImageStore(tmp_path / 'images', 'linux/arm/v7')
    blobs = {}
    def descriptor(data, media):
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        blobs[digest] = data
        return {'digest': digest, 'size': len(data), 'mediaType': media}
    config = descriptor(json.dumps({'os': 'linux', 'architecture': 'arm', 'config': {'Cmd': ['hello']}}).encode(), 'config')
    archive = layer(tmp_path, [('hello', 'world', '')]).read_bytes()
    ld = descriptor(archive, 'application/vnd.oci.image.layer.v1.tar+gzip')
    manifest = descriptor(json.dumps({'schemaVersion': 2, 'config': config, 'layers': [ld]}).encode(), 'manifest')
    index = json.dumps({'manifests': [dict(manifest, platform={'os': 'linux', 'architecture': 'arm', 'variant': 'v7'})]}).encode()
    calls = []
    def request(url, token=None):
        calls.append((url, token))
        if url.startswith('https://auth.example/token?'):
            assert 'repository%3A' in url
            return io.BytesIO(b'{"token":"test-token"}')
        if not token:
            raise HTTPError(url, 401, 'auth', {'WWW-Authenticate': 'Bearer realm="https://auth.example/token",service="registry"'}, None)
        if url.endswith('/manifests/latest'):
            return io.BytesIO(index)
        return io.BytesIO(blobs[url.rsplit('/', 1)[-1]])
    monkeypatch.setattr(store, '_request', request)
    return store, calls, blobs, ld


@pytest.mark.parametrize('registry', ['docker.io', 'ghcr.io'])
def test_registry_pull_cache_and_policy(tmp_path, monkeypatch, registry):
    store, calls, _, _ = registry_fixture(tmp_path, monkeypatch, registry)
    ref = registry + '/org/app:latest'
    result = store.pull(ref)
    assert (result.rootfs / 'hello').read_text() == 'world'
    assert result.config['Cmd'] == ['hello']
    count = len(calls)
    assert store.pull(ref, 'Never').rootfs == result.rootfs
    assert len(calls) == count
    store.pull(ref, 'Always')
    assert len(calls) > count
    with pytest.raises(RuntimeError, match='not cached'):
        store.pull(registry + '/org/other', 'Never')


def test_corrupt_layer_never_published(tmp_path, monkeypatch):
    store, _, blobs, ld = registry_fixture(tmp_path, monkeypatch)
    blobs[ld['digest']] = b'corrupt'
    with pytest.raises(RuntimeError, match='mismatch'):
        store.pull('org/app')
    assert not list(store.directory.rglob('config.json'))
    assert not list(store.directory.rglob('.pull-*'))


def test_missing_platform(tmp_path, monkeypatch):
    store, _, _, _ = registry_fixture(tmp_path, monkeypatch)
    store.target = 'linux/arm64/v8'
    with pytest.raises(RuntimeError, match='no linux/arm64/v8'):
        store.pull('org/app')


def test_runtime_uses_private_copy_and_pins_exec(tmp_path, monkeypatch):
    import asyncio
    from pocketkube.runtime.images import Image

    source = tmp_path / 'source'
    source.mkdir()
    (source / 'file').write_text('original')
    (source / 'bin').mkdir()
    (source / 'bin' / 'sh').write_text('placeholder')
    (source / 'usr' / 'bin').mkdir(parents=True)
    (source / 'usr' / 'bin' / 'env').write_text('placeholder')
    runtime = RawProotRuntime()
    runtime.images = ImageStore(tmp_path / 'images')
    pulls = []
    def pull(image, policy):
        pulls.append((image, policy))
        return Image(source, {'Entrypoint': ['entry'], 'Cmd': ['default'],
                              'Env': ['A=image'], 'WorkingDir': '/app'})
    monkeypatch.setattr(runtime.images, 'pull', pull)
    commands = []
    class Process:
        returncode = None
        stderr = None
        stdout = None
        async def wait(self):
            if self.returncode is None:
                raise asyncio.TimeoutError()
            return self.returncode
        def terminate(self):
            self.returncode = 0
    async def spawn(*args, **kwargs):
        commands.append(args)
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    pod = {'spec': {'containers': [{'image': 'ghcr.io/org/app:v1', 'args': ['override'],
                                   'env': [{'name': 'A', 'value': 'pod'}]}]}}
    async def run():
        await runtime.start_pod('default', 'app', pod)
        copy = runtime._pod_images[('default', 'app')].rootfs
        assert copy != source
        (copy / 'file').write_text('changed')
        assert (source / 'file').read_text() == 'original'
        assert commands[0][-3:] == ('A=pod', 'entry', 'override')
        assert commands[0][commands[0].index('-w') + 1] == '/app'
        await runtime.exec_stream('default', 'app', pod, ['echo', 'test'])
        assert len(pulls) == 1
        assert str(copy) in commands[1]
        await runtime.stop_pod('default', 'app', pod)
        assert not copy.exists()
    asyncio.run(run())
