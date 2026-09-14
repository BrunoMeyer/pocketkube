# PocketKube

PocketKube is a tiny, intentionally incomplete Kubernetes-compatible API server for running simple Linux userspace processes on resource-constrained devices, especially **Android + Termux without root**.

It is a simulator/compatibility layer, not a real Kubernetes distribution or a security boundary. The goal is to keep enough Kubernetes API behavior for experiments with `kubectl` while avoiding kubelet, containerd, cgroups, CNI, systemd, privileged namespaces, and other kernel assumptions that are troublesome on stock Android phones.

## Implemented subset

- Pods: create, get/list, delete
- Deployments: create, get/list, delete, basic one-shot replica creation
- Namespaces: basic create/get/list/delete
- `kubectl exec` using Kubernetes WebSocket remote-command channels
- one container per Pod
- raw rootless `proot` backend for Termux
- Docker backend for desktop development

## Architecture

```text
kubectl
   |
   | Kubernetes-compatible HTTP/WebSocket API
   v
PocketKube (Python + Starlette)
   |
   | runtime adapter
   +----------------------+
   |                      |
   v                      v
raw PRoot              Docker
(Android/Termux)       (desktop testing)
   |
   v
pre-extracted Alpine rootfs
```

The raw PRoot backend currently supports only Alpine images and maps `alpine` / `alpine:*` to one pre-extracted Alpine root filesystem. It does **not** pull arbitrary OCI images yet.

## Termux installation

Install the dependencies available in your Termux repository:

```sh
pkg update
pkg install python proot wget tar busybox git
```

`proot-distro` is **not required**.

This is important for legacy Android 5/6 Termux installations where `proot` exists but `proot-distro` may not be packaged.

Create a Python environment and install PocketKube:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e . --no-deps
```

If `venv` is unavailable in the old Termux Python package, installing directly with `pip install -e . --no-deps` is sufficient for experimentation.

## Prepare the Alpine rootfs

The included helper detects ARMv7 vs AArch64 and extracts an Alpine minirootfs:

```sh
./scripts/setup-alpine-rootfs.sh
```

By default the rootfs is placed at:

```text
~/rootfs/alpine
```

You can override it:

```sh
POCKETKUBE_PROOT_ROOTFS=$HOME/my-rootfs ./scripts/setup-alpine-rootfs.sh
```

### Why the script uses BusyBox tar

Legacy Android 5 Termux GNU tar builds can fail while extracting Alpine symlinks with errors such as:

```text
tar: ./bin/sh: Cannot change mode ... No such file or directory
```

The setup script intentionally uses `busybox tar` to avoid that old extraction bug.

## Verify raw PRoot manually

On old Android Termux, do **not** unset `LD_LIBRARY_PATH` before launching `proot`. The host-side PRoot binary may need `$PREFIX/lib` to load libraries such as `libtalloc` and `libandroid-support`.

Use:

```sh
export LD_LIBRARY_PATH="$PREFIX/lib"
unset LD_PRELOAD

proot \
  --link2symlink \
  -0 \
  -r "$HOME/rootfs/alpine" \
  -w / \
  /bin/sh -c '
    unset LD_LIBRARY_PATH LD_PRELOAD
    echo "=== PocketKube PRoot test ==="
    cat /etc/alpine-release
    uname -m
    id
    echo "Hello from Alpine"
  '
```

The PocketKube raw PRoot backend performs this host/guest environment handling automatically.

## kubectl exec compatibility

PocketKube implements Kubernetes remote command over **WebSockets only**. It does not implement the older SPDY transport.

On kubectl 1.29/1.30, and on any client where WebSocket remote-command is not enabled by default, export:

```sh
export KUBECTL_REMOTE_COMMAND_WEBSOCKETS=true
```

Kubernetes 1.31 enabled the WebSocket transition by default, but explicitly setting the variable is harmless and makes the intended transport clear.

The server uses `websockets==13.0` explicitly so WebSocket support is present even on older Python/Termux environments.

First test exec without a TTY:

```sh
KUBECTL_REMOTE_COMMAND_WEBSOCKETS=true \
  kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig \
  exec alpine -- cat /etc/alpine-release
```

PocketKube does not yet provide a real PTY, so `-t` is not required and interactive terminal behavior is intentionally incomplete. `-i` can be used for stdin streaming.

## Start PocketKube

On the Android device:

```sh
pocketkube serve --runtime proot --rootfs "$HOME/rootfs/alpine"
```

For access from another machine on the same network:

```sh
pocketkube serve \
  --runtime proot \
  --rootfs "$HOME/rootfs/alpine" \
  --host 0.0.0.0 \
  --port 8443
```

PocketKube has no authentication or TLS yet, so do not expose that listener to an untrusted network.

## Configure kubectl

You can generate a kubeconfig on the machine where PocketKube is running:

```sh
pocketkube kubeconfig --server http://127.0.0.1:8443
```

Or, if `kubectl` runs on another machine, generate it there with the Android device's LAN address:

```sh
pocketkube kubeconfig \
  --server http://PHONE_IP:8443 \
  --output ~/.kube/pocketkube.kubeconfig
```

Then:

```sh
kubectl get pods --kubeconfig ~/.kube/pocketkube.kubeconfig
```

## Run the included Pod

```sh
kubectl apply \
  -f examples/pod.yaml \
  --kubeconfig ~/.kube/pocketkube.kubeconfig \
  --validate=false
```

Check it:

```sh
kubectl get pods --kubeconfig ~/.kube/pocketkube.kubeconfig
kubectl describe pod alpine --kubeconfig ~/.kube/pocketkube.kubeconfig
```

Then test exec:

```sh
kubectl exec \
  --kubeconfig ~/.kube/pocketkube.kubeconfig \
  alpine -- cat /etc/alpine-release
```

and:

```sh
kubectl exec \
  --kubeconfig ~/.kube/pocketkube.kubeconfig \
  alpine -- uname -a
```

Interactive stdin is implemented:

```sh
kubectl exec -i \
  --kubeconfig ~/.kube/pocketkube.kubeconfig \
  alpine -- /bin/sh
```

A real PTY is not implemented yet, so `kubectl exec -it` will not behave exactly like Kubernetes.

## Run the included Deployment

The Deployment example also uses Alpine so it works with the current raw PRoot backend:

```sh
kubectl apply \
  -f examples/deployment.yaml \
  --kubeconfig ~/.kube/pocketkube.kubeconfig \
  --validate=false

kubectl get deployments,pods \
  --kubeconfig ~/.kube/pocketkube.kubeconfig
```

The controller creates the requested replicas once. It does not yet reconcile deleted or failed replicas.

## Runtime selection

Raw PRoot is the default:

```sh
pocketkube serve
```

Equivalent explicit command:

```sh
pocketkube serve --runtime proot --rootfs ~/rootfs/alpine
```

For desktop development with Docker:

```sh
pocketkube serve --runtime docker
```

Environment variables are also supported:

```sh
export POCKETKUBE_RUNTIME=proot
export POCKETKUBE_PROOT_ROOTFS=$HOME/rootfs/alpine
pocketkube serve
```

## Current raw PRoot semantics

A Pod is represented primarily by a PRoot process plus a shared root filesystem:

```text
Pod
 |- Kubernetes metadata/status held by PocketKube
 |- PRoot process PID
 `- Alpine rootfs: ~/rootfs/alpine
```

`kubectl exec` starts another PRoot process against the same rootfs. There are no real container namespaces or cgroups.

Because the current implementation shares one Alpine rootfs across all Pods, filesystem writes are also shared. This is intentional for the first lightweight version.

## Supported API subset

Core `v1`:

- Namespaces: GET/LIST/CREATE/DELETE
- Pods: GET/LIST/CREATE/DELETE
- Pods exec: WebSocket `v5.channel.k8s.io`, with v4 fallback

`apps/v1`:

- Deployments: GET/LIST/CREATE/DELETE

Discovery endpoints:

- `/version`
- `/api`
- `/api/v1`
- `/apis`
- `/apis/apps/v1`

## Important limitations

PocketKube currently does not implement:

- arbitrary OCI/Docker image pulling
- Docker image ENTRYPOINT/CMD discovery in the PRoot backend
- real filesystem isolation between Pods
- Services, DNS, CNI, Pod IPs, NetworkPolicy
- cgroups / CPU / memory limits
- Linux namespace isolation
- volumes / PVCs
- Secrets / ConfigMaps
- health probes
- multi-node scheduling
- ReplicaSets as persisted objects
- Deployment reconciliation / rolling updates
- server-side apply / strategic merge patch
- logs / attach / port-forward
- init containers or multiple containers per Pod
- true PTY allocation for `kubectl exec -t`
- authentication, authorization, TLS, admission control
- persistence across PocketKube server restarts

## Recommended next steps

1. OCI registry client + layer/whiteout extraction for arbitrary images.
2. Per-Pod copy-on-write-ish rootfs strategy that does not require overlayfs.
3. Persistent SQLite state.
4. Deployment reconciliation loop.
5. `kubectl logs`.
6. PTY support for `kubectl exec -it`.
7. User-space port mapping / lightweight Services.
8. Tiny remote node agent so multiple Android phones can act as simulated Kubernetes nodes.


## WebSocket exec transport

PocketKube uses Kubernetes remote-command WebSockets for `kubectl exec`. The server is started with Uvicorn's `websockets` backend because Kubernetes requires the negotiated subprotocol (`v5.channel.k8s.io`) to be returned in the HTTP 101 response.

With modern kubectl (including v1.37), no special environment variable should normally be required. If you have a proxy configured, ensure the PocketKube host is included in `NO_PROXY`, because proxies that reject WebSocket upgrades can cause kubectl to fall back to its legacy SPDY POST path.


### Legacy Termux packaging

PocketKube 0.2.4 uses `setup.py` rather than a mandatory `pyproject.toml`, so editable installs work with older setuptools releases. Dependencies can be installed separately and then PocketKube can be linked with:

```bash
pip install -e . --no-deps
```

### Exec and TTY

`kubectl exec` uses the Kubernetes WebSocket remote-command protocol. PocketKube does not yet allocate a real PTY; when `-t` is requested it merges stderr into stdout as Kubernetes expects, but terminal-specific behavior is still limited.
