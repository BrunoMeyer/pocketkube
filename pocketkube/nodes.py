"""Read-only description of PocketKube's single local execution node."""
from __future__ import annotations

import ipaddress
import os
import platform
import re
import socket
from pathlib import Path

from . import __version__
from .models import ensure_metadata
from .runtime.images import host_platform


def _host_identifier(*paths):
    """Required NodeSystemInfo strings stay present on restricted Android hosts."""
    for path in paths:
        try:
            value = Path(path).read_text().strip()
        except (OSError, UnicodeError):
            continue
        if value:
            return value
    return ''


def local_node(runtime_name):
    configured = os.environ.get('POCKETKUBE_NODE_NAME')
    name = configured or re.sub(r'[^a-z0-9.-]', '-', socket.gethostname().lower()).strip('-.') or 'pocketkube'
    if len(name) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                              for part in name.split('.')):
        raise ValueError('POCKETKUBE_NODE_NAME must be a valid DNS subdomain')
    arch = host_platform().split('/')[1]
    addresses = [{'type': 'Hostname', 'address': name}]
    address = os.environ.get('POCKETKUBE_NODE_IP')
    if address:
        addresses.append({'type': 'InternalIP', 'address': str(ipaddress.ip_address(address))})
    capacity = {}
    cpus = os.cpu_count()
    if cpus:
        capacity['cpu'] = str(cpus)
    try:
        memory = os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
        if memory > 0:
            capacity['memory'] = str(memory)
    except (AttributeError, OSError, ValueError):
        pass
    os_image = platform.system()
    try:
        for line in Path('/etc/os-release').read_text().splitlines():
            if line.startswith('PRETTY_NAME='):
                os_image = line.split('=', 1)[1].strip('"\'')
                break
    except OSError:
        pass
    if os.environ.get('ANDROID_ROOT'):
        os_image = 'Android / Termux'
    node = ensure_metadata({
        'apiVersion': 'v1', 'kind': 'Node',
        'metadata': {'name': name, 'resourceVersion': '1', 'labels': {
            'kubernetes.io/hostname': name, 'kubernetes.io/os': 'linux',
            'kubernetes.io/arch': arch, 'node-role.kubernetes.io/pocketkube': '',
        }},
        'spec': {},
        'status': {
            'addresses': addresses, 'capacity': capacity, 'allocatable': dict(capacity),
            'nodeInfo': {
                'architecture': arch, 'operatingSystem': 'linux',
                'bootID': _host_identifier('/proc/sys/kernel/random/boot_id'),
                'machineID': _host_identifier('/etc/machine-id', '/var/lib/dbus/machine-id'),
                'systemUUID': _host_identifier('/sys/class/dmi/id/product_uuid'),
                # PocketKube does not run kube-proxy, but older typed clients
                # require this field to be a non-null string.
                'kubeProxyVersion': '',
                'osImage': os_image, 'kernelVersion': platform.release(),
                'kubeletVersion': 'v1.31.0-pocketkube',
                'containerRuntimeVersion': 'pocketkube-' + runtime_name + '://' + __version__,
            },
        },
    })
    created = node['metadata']['creationTimestamp']
    node['status']['conditions'] = [{
        'type': 'Ready', 'status': 'True', 'reason': 'PocketKubeAvailable',
        'message': 'PocketKube API is running. Runtime health and resource pressure are not monitored.',
        'lastHeartbeatTime': created, 'lastTransitionTime': created,
    }]
    return node


def select_fields(items, selector, supported):
    """Equality field selectors used by kubectl describe node's Pod query."""
    clauses = []
    for clause in selector.split(',') if selector else []:
        match = re.fullmatch(r'\s*([^!=\s]+)\s*(!=|==|=)\s*(.*?)\s*', clause)
        if not match or match[1] not in supported:
            raise ValueError('unsupported field selector: ' + clause)
        clauses.append(match.groups())
    def matches(item):
        for field, operator, expected in clauses:
            value = item
            for part in field.split('.'):
                value = value.get(part, {}) if isinstance(value, dict) else {}
            value = value if isinstance(value, str) else ''
            if (value == expected) == (operator == '!='):
                return False
        return True
    return [item for item in items if matches(item)]


def node_table(items):
    from datetime import datetime, timezone
    columns = [
        ('Name', 'name', 0), ('Status', '', 0), ('Roles', '', 0), ('Age', '', 0), ('Version', '', 0),
        ('Internal-IP', '', 1), ('External-IP', '', 1), ('OS-Image', '', 1),
        ('Kernel-Version', '', 1), ('Container-Runtime', '', 1),
    ]
    rows = []
    for node in items:
        metadata, status = node['metadata'], node['status']
        info = status['nodeInfo']
        addresses = {a['type']: a['address'] for a in status['addresses']}
        seconds = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(
            metadata['creationTimestamp'].replace('Z', '+00:00'))).total_seconds()))
        age = str(seconds) + 's' if seconds < 60 else (str(seconds // 60) + 'm' if seconds < 3600
               else str(seconds // 3600) + 'h' if seconds < 86400 else str(seconds // 86400) + 'd')
        ready = any(c['type'] == 'Ready' and c['status'] == 'True' for c in status['conditions'])
        rows.append({'cells': [metadata['name'], 'Ready' if ready else 'NotReady', 'pocketkube', age,
                              info['kubeletVersion'], addresses.get('InternalIP', '<none>'),
                              addresses.get('ExternalIP', '<none>'), info['osImage'], info['kernelVersion'],
                              info['containerRuntimeVersion']], 'object': node})
    return {'apiVersion': 'meta.k8s.io/v1', 'kind': 'Table', 'metadata': {'resourceVersion': '1'},
            'columnDefinitions': [{'name': name, 'type': 'string', 'format': fmt, 'priority': priority}
                                  for name, fmt, priority in columns], 'rows': rows}


class NodePatchConflict(ValueError):
    pass


def patch_node_labels(node, patch):
    """Apply the metadata.labels subset of JSON/strategic merge patch atomically."""
    import copy
    if not isinstance(patch, dict) or set(patch) - {'metadata', 'kind', 'apiVersion'}:
        raise ValueError('only node metadata.labels patches are supported')
    for key, expected in (('kind', 'Node'), ('apiVersion', 'v1')):
        if key in patch and patch[key] != expected:
            raise ValueError('invalid node ' + key)
    metadata = patch.get('metadata', {})
    if not isinstance(metadata, dict) or set(metadata) - {'labels', 'resourceVersion', 'name'}:
        raise ValueError('only metadata.labels and resourceVersion may be changed')
    if 'name' in metadata and metadata['name'] != node['metadata']['name']:
        raise ValueError('node name is immutable')
    if 'resourceVersion' in metadata and metadata['resourceVersion'] != node['metadata']['resourceVersion']:
        raise NodePatchConflict('node resourceVersion has changed; fetch the node and retry')
    labels = metadata.get('labels', {})
    if labels is not None and not isinstance(labels, dict):
        raise ValueError('metadata.labels must be an object or null')
    token = r'[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?'
    for key, value in (labels or {}).items():
        prefix, sep, name = key.rpartition('/')
        name = name if sep else key
        if not re.fullmatch(token, name):
            raise ValueError('invalid label key: ' + key)
        if sep and (len(prefix) > 253 or not prefix or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in prefix.split('.'))):
            raise ValueError('invalid label prefix: ' + key)
        if value is not None and (not isinstance(value, str) or (value and not re.fullmatch(token, value))):
            raise ValueError('invalid label value for ' + key)
    result = copy.deepcopy(node)
    updated = result['metadata'].setdefault('labels', {})
    if labels is None:
        updated.clear()
    else:
        for key, value in labels.items():
            if value is None:
                updated.pop(key, None)
            else:
                updated[key] = value
    result['metadata']['resourceVersion'] = str(int(node['metadata']['resourceVersion']) + 1)
    return result
