"""Small, dependency-free OCI/Docker Registry v2 image store."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

ACCEPT = ', '.join([
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
])


@dataclass
class Image:
    rootfs: Path
    config: dict


def host_platform():
    machine = platform.machine().lower()
    return {'armv7l': 'linux/arm/v7', 'armv8l': 'linux/arm/v7',
            'armv6l': 'linux/arm/v6', 'aarch64': 'linux/arm64/v8',
            'arm64': 'linux/arm64/v8', 'x86_64': 'linux/amd64',
            'i686': 'linux/386', 'i386': 'linux/386'}.get(machine, 'linux/' + machine)


def reference(value):
    if '://' in value:
        raise RuntimeError('image references must not contain a URL scheme')
    name, sep, digest = value.partition('@')
    last = name.rsplit('/', 1)[-1]
    tag = 'latest'
    if ':' in last:
        name, tag = name.rsplit(':', 1)
    parts = name.split('/')
    registry = 'docker.io'
    if len(parts) > 1 and ('.' in parts[0] or ':' in parts[0] or parts[0] == 'localhost'):
        registry = parts.pop(0)
    if registry in ('index.docker.io', 'registry-1.docker.io'):
        registry = 'docker.io'
    if registry == 'docker.io' and len(parts) == 1:
        parts.insert(0, 'library')
    if not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?', registry) or any(
        not re.fullmatch(r'[a-z0-9]+(?:[._-]+[a-z0-9]+)*', p) for p in parts
    ):
        raise RuntimeError('invalid image reference: ' + value)
    ref = digest if sep else tag
    if sep:
        check_digest(ref)
    elif not re.fullmatch(r'[\w][\w.-]{0,127}', ref, flags=re.ASCII):
        raise RuntimeError('invalid image tag')
    return registry, '/'.join(parts), ref


def check_digest(digest):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise RuntimeError('unsupported or invalid image digest: ' + str(digest))


def verify(data, digest):
    check_digest(digest)
    if hashlib.sha256(data).hexdigest() != digest.split(':')[1]:
        raise RuntimeError('image digest mismatch: ' + digest)


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != 'https':
            raise RuntimeError('registry redirected to an insecure URL')
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result and urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            result.remove_header('Authorization')
        return result


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def guest_path(root, name, follow_final=False):
    """Resolve archive paths inside the guest, including absolute guest symlinks."""
    name = name.lstrip('/')
    if '..' in name.split('/'):
        raise RuntimeError('unsafe layer path: ' + name)
    pending = name.split('/')
    resolved = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            if not resolved:
                raise RuntimeError('layer symlink escapes rootfs')
            resolved.pop()
            continue
        path = root.joinpath(*resolved, part)
        if path.is_symlink() and (pending or follow_final):
            links += 1
            if links > 40:
                raise RuntimeError('layer symlink loop')
            target = os.readlink(path)
            if target.startswith('/'):
                resolved = []
            pending = target.split('/') + pending
        else:
            resolved.append(part)
    return root.joinpath(*resolved)


def apply_layer(root, archive):
    with tarfile.open(archive, 'r:*') as tar:
        members = tar.getmembers()
        # Whiteouts affect only lower layers, regardless of archive ordering.
        for member in members:
            path = guest_path(root, member.name)
            if path.name == '.wh..wh..opq':
                parent = guest_path(root, str(Path(member.name).parent), True)
                if parent.is_dir():
                    for child in parent.iterdir():
                        remove(child)
            elif path.name.startswith('.wh.'):
                target = path.name[4:]
                if target in ('', '.', '..'):
                    raise RuntimeError('unsafe whiteout path')
                remove(path.with_name(target))
        for member in members:
            path = guest_path(root, member.name)
            if path.name.startswith('.wh.'):
                continue
            if path == root:
                if not member.isdir():
                    raise RuntimeError('layer replaces root directory')
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                if path.is_symlink() or (path.exists() and not path.is_dir()):
                    remove(path)
                path.mkdir(exist_ok=True)
                path.chmod(member.mode | 0o700)
            elif member.isfile():
                remove(path)
                with tar.extractfile(member) as src, path.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                path.chmod(member.mode | 0o600)
            elif member.issym():
                remove(path)
                path.symlink_to(member.linkname)
            elif member.islnk():
                source = guest_path(root, member.linkname, True)
                if not source.is_file():
                    raise RuntimeError('invalid layer hardlink: ' + member.linkname)
                remove(path)
                shutil.copyfile(source, path)
                path.chmod(member.mode | 0o600)
            elif member.isdev() or member.isfifo():
                # PRoot supplies host devices; unprivileged extraction cannot create these.
                continue
            else:
                raise RuntimeError('unsupported layer entry: ' + member.name)


class ImageStore:
    def __init__(self, directory=None, target=None):
        self.directory = Path(directory or os.environ.get('POCKETKUBE_IMAGE_DIR', '~/.pocketkube/images')).expanduser()
        self.target = target or os.environ.get('POCKETKUBE_PLATFORM') or host_platform()
        self.lock = threading.Lock()
        self.opener = build_opener(SafeRedirect())

    def _request(self, url, token=None):
        headers = {'Accept': ACCEPT}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        return self.opener.open(Request(url, headers=headers), timeout=60)

    def pull(self, value, policy='IfNotPresent'):
        if policy not in ('Always', 'IfNotPresent', 'Never'):
            raise RuntimeError('unsupported imagePullPolicy: ' + policy)
        with self.lock:
            return self._pull(value, policy)

    def _pull(self, value, policy):
        registry, repo, ref = reference(value)
        base = self.directory / registry / repo
        key = hashlib.sha256((ref + '/' + self.target).encode()).hexdigest()
        pointer = base / ('ref-' + key + '.json')
        if policy != 'Always' and pointer.exists():
            digest = json.loads(pointer.read_text())
            check_digest(digest)
            cached = base / digest.replace(':', '-')
            if (cached / 'config.json').exists() and (cached / 'rootfs').is_dir():
                return Image(cached / 'rootfs', json.loads((cached / 'config.json').read_text()))
        if policy == 'Never':
            raise RuntimeError('image is not cached: ' + value)
        host = 'registry-1.docker.io' if registry == 'docker.io' else registry
        url = 'https://' + host + '/v2/' + repo
        token = None

        def fetch(path):
            nonlocal token
            try:
                return self._request(url + path, token)
            except HTTPError as exc:
                if exc.code != 401:
                    raise RuntimeError('registry request failed (HTTP %s): %s' % (exc.code, value)) from exc
                challenge = exc.headers.get('WWW-Authenticate', '')
                if not challenge.lower().startswith('bearer '):
                    raise RuntimeError('registry requires unsupported authentication') from exc
                fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
                realm = fields.get('realm', '')
                if urlparse(realm).scheme != 'https':
                    raise RuntimeError('registry token endpoint must use HTTPS')
                query = urlencode({'service': fields.get('service', host), 'scope': 'repository:' + repo + ':pull'})
                with self._request(realm + ('&' if '?' in realm else '?') + query) as response:
                    auth = json.load(response)
                token = auth.get('token') or auth.get('access_token')
                if not token:
                    raise RuntimeError('registry did not return an anonymous pull token')
                return self._request(url + path, token)

        def manifest(identity):
            with fetch('/manifests/' + identity) as response:
                data = response.read()
            if identity.startswith('sha256:'):
                verify(data, identity)
            return json.loads(data), 'sha256:' + hashlib.sha256(data).hexdigest()

        document, digest = manifest(ref)
        target = self.target.split('/')
        if len(target) not in (2, 3):
            raise RuntimeError('platform must be os/architecture[/variant]')
        def matches(p):
            return p.get('os') == target[0] and p.get('architecture') == target[1] and (
                len(target) == 2 or p.get('variant') == target[2] or
                (target[1:] == ['arm64', 'v8'] and not p.get('variant')))
        for _ in range(4):
            if 'manifests' not in document:
                break
            candidates = [m for m in document['manifests'] if matches(m.get('platform', {}))]
            if not candidates:
                raise RuntimeError('image %s has no %s variant' % (value, self.target))
            document, digest = manifest(candidates[0]['digest'])
        if document.get('schemaVersion') != 2 or 'layers' not in document:
            raise RuntimeError('unsupported image manifest')
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.pull-', dir=str(base)) as temporary:
            work = Path(temporary)
            def blob(descriptor, destination):
                check_digest(descriptor['digest'])
                hasher = hashlib.sha256()
                size = 0
                with fetch('/blobs/' + descriptor['digest']) as response, destination.open('wb') as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                if 'sha256:' + hasher.hexdigest() != descriptor['digest'] or size != descriptor['size']:
                    raise RuntimeError('image blob digest or size mismatch')
            blob(document['config'], work / 'image.json')
            metadata = json.loads((work / 'image.json').read_text())
            if metadata.get('os') != target[0] or metadata.get('architecture') != target[1]:
                raise RuntimeError('image config does not match platform ' + self.target)
            if metadata.get('variant') and not matches(metadata):
                raise RuntimeError('image config variant does not match ' + self.target)
            root = work / 'rootfs'
            root.mkdir()
            for layer in document['layers']:
                media = layer.get('mediaType', '')
                if media not in ('application/vnd.oci.image.layer.v1.tar', 'application/vnd.oci.image.layer.v1.tar+gzip', 'application/vnd.docker.image.rootfs.diff.tar.gzip'):
                    raise RuntimeError('unsupported image layer compression/media type: ' + media)
                blob(layer, work / 'layer.tar')
                apply_layer(root, work / 'layer.tar')
            config = metadata.get('config') or {}
            (work / 'config.json').write_text(json.dumps(config))
            (work / 'image.json').unlink()
            (work / 'layer.tar').unlink(missing_ok=True)
            cached = base / digest.replace(':', '-')
            if not cached.exists():
                os.rename(work, cached)
        temp_pointer = pointer.with_suffix('.tmp')
        temp_pointer.write_text(json.dumps(digest))
        temp_pointer.replace(pointer)
        return Image(cached / 'rootfs', config)
