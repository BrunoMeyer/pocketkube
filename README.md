# PocketKube

PocketKube is a tiny, intentionally incomplete Kubernetes-compatible API server for running simple Linux userspace processes on resource-constrained devices, especially **Android + Termux without root**.

It is a simulator/compatibility layer, not a real Kubernetes distribution or a security boundary. The goal is to keep enough Kubernetes API behavior for experiments with `kubectl` while avoiding kubelet, containerd, cgroups, CNI, systemd, privileged namespaces, and other kernel assumptions that are troublesome on stock Android phones.

## Implemented subset

- Pods: create, get/list, delete
- Deployments: create, get/list, delete, basic one-shot replica creation
- Namespaces: basic create/get/list/delete
- Nodes: get/list the single local PocketKube node
- `kubectl top nodes`: host CPU and memory usage
- `kubectl exec` using Kubernetes WebSocket remote-command channels
- `kubectl logs` with follow, tail, timestamps, time filtering, and byte limits
- `kubectl port-forward` over WebSocket for TCP connections
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
OCI image rootfs
```

The raw PRoot backend pulls public Docker Hub and GHCR images using the Registry v2 API. It selects the host Linux architecture automatically, including `linux/arm/v7` on `armv7l`, verifies SHA-256 digests, and applies layers and whiteouts in order. Tags and digest references are supported.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: nginx
spec:
  containers:
    - name: nginx
      image: nginx:alpine
```

Start with `pocketkube serve --runtime proot` to enable registry images. References such as `ghcr.io/OWNER/IMAGE:TAG` work for public packages with a compatible platform. Image ENTRYPOINT and CMD provide defaults; Kubernetes `command` overrides ENTRYPOINT and `args` overrides CMD independently. Image environment, working directory and user are also read, with Pod `env` and `workingDir` taking precedence.

Images are cached under `~/.pocketkube/images/<registry>/<repository>/sha256-<digest>/`. Each Pod receives a separate full filesystem copy, removed when the Pod is deleted. Allow disk space for both the cache and each Pod. `exec` uses the same copy as the running Pod.

`imagePullPolicy` supports `Always`, `IfNotPresent`, and `Never`. Omitted/latest tags default to `Always`; other tags and digests default to `IfNotPresent`. Set `POCKETKUBE_IMAGE_DIR` to relocate the cache, or `POCKETKUBE_PLATFORM=linux/arm/v7` to select a platform explicitly (this does not provide CPU emulation).

This initial puller supports public anonymous pulls, SHA-256, and uncompressed/gzip layers. Private registry credentials, imagePullSecrets, and zstd layers are not yet supported. Images with `/bin/sh` use the normal environment wrapper; shell-less images run directly without requiring `/bin/sh` or `/usr/bin/env`. Device nodes are skipped during rootless extraction. Kernel-dependent images still face PRoot limitations.

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

## Optional legacy Alpine rootfs

Registry mode does not need this step. For offline legacy use, the included helper detects ARMv7 vs AArch64 and extracts an Alpine minirootfs:

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

Modern Termux should launch PRoot without a forced `LD_LIBRARY_PATH`. PocketKube removes inherited `LD_LIBRARY_PATH` when `PREFIX` is set, avoiding collisions with Android system libraries (including the `libunwindstack.so` / `Xzs_Construct` linker error).

For legacy Android 5/6 Termux installations that require `$PREFIX/lib` to find `libtalloc` or `libandroid-support`, explicitly set:

```sh
export POCKETKUBE_PROOT_LD_LIBRARY_PATH="$PREFIX/lib"
```

Leave this override unset on modern Termux. An empty override explicitly clears the host library path. The setting applies to Pod startup and exec, including interactive terminals.

Use:

```sh
unset LD_LIBRARY_PATH LD_PRELOAD
# Legacy Android 5/6 only, if required: export LD_LIBRARY_PATH="$PREFIX/lib"

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

Use `-it` for an interactive shell with a real pseudo-terminal, or `-i` alone for piped stdin. Terminal resize events and Ctrl-C are forwarded to the terminal.

## Start PocketKube

On the Android device:

```sh
pocketkube serve --runtime proot
```

For access from another machine on the same network:

```sh
pocketkube serve \
  --runtime proot \
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

## Inspect the local node

```sh
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig get nodes
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig get nodes -o wide
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig describe node
```

PocketKube exposes one node named after the host. Set `POCKETKUBE_NODE_NAME` before starting the server to choose a DNS-compatible name, and optionally `POCKETKUBE_NODE_IP` to advertise a host IPv4/IPv6 address. The IP is not guessed when omitted. New Pods receive this node's `spec.nodeName`; requests naming a different node are rejected.

Node data includes host CPU/memory capacity, architecture, kernel, OS, and the PocketKube runtime adapter version. Capacity is informational, not an enforced allocation or scheduling guarantee. `Ready` means the PocketKube API is available; runtime health and resource pressure are not monitored. Node identity and age reset on server restart. Watches, node spec/status mutations, and node leases are not implemented; `describe node` may note that its lease is unavailable. Event lists are empty because events are not recorded yet.

Node labels can be added, overwritten, or removed:

```sh
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig label node localhost mentored=ready
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig label node localhost mentored=busy --overwrite
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig label node localhost mentored-
```

Replace `localhost` with the name shown by `get nodes`. Label updates support JSON merge patch and strategic merge patch, resource-version conflict checks, and `--dry-run=server`. Labels remain in memory until PocketKube restarts; they do not add scheduling or node-selector enforcement.

## Node resource usage

```sh
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig top nodes
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig top node NODE_NAME
```

PocketKube serves `metrics.k8s.io/v1` and `v1beta1` directly; no metrics-server installation is needed. CPU is the rate of aggregate host CPU time over a 250 ms sample, excluding idle and I/O wait. Memory is a host working-set estimate: `MemTotal - MemFree - Inactive(file)`. Samples are shared and cached for one second.

These measurements cover the entire machine running PocketKube, including other applications. They are not per-Pod measurements or cgroup accounting, and can differ from kubelet/metrics-server figures. For the Docker backend they describe the PocketKube host, not a remote Docker daemon. CPU/memory allocatable values equal the reported host capacity because PocketKube reserves no resources; neither value enforces limits.

By default, Linux/Android must permit reading `/proc/stat` and `/proc/meminfo`. Restricted devices return a metrics-unavailable error instead of fabricated usage. `kubectl top pods`, metrics watches, and label selectors are not supported.

### Restricted Android metrics

If Android denies `/proc/stat`, opt in to a narrower measurement before starting PocketKube:

```sh
POCKETKUBE_METRICS_SCOPE=visible-processes pocketkube serve --runtime proot
```

Then use `kubectl top nodes` normally. This mode measures readable processes owned by the PocketKube/Termux user, including other Termux sessions. It does **not** measure whole-device usage. CPU counts only processes present in both snapshots; short-lived processes can be missed. Memory is summed resident memory (RSS), which may double-count shared pages, rather than the host working-set estimate. Percentages still use host capacity. Responses include a scope annotation and a warning shown by kubectl. No root or Android security-policy changes are required, but same-user `/proc/PID/stat` files must be readable. Leave `POCKETKUBE_METRICS_SCOPE` unset (or set it to `host`) for full host metrics where permitted.

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

For an interactive terminal:

```sh
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig exec -it nginx -- /bin/sh
```

Ctrl-C interrupts the foreground command; it normally leaves the shell open. Type `exit` or press Ctrl-D on an empty command line to leave the shell. Disconnecting the client terminates the exec session.

## Run nginx with PRoot

Use `examples/nginx.yaml` to run `nginx:alpine` on port **8080**. The example updates nginx's listener before calling its original entrypoint. PRoot's simulated root does not grant permission to bind privileged host ports such as 80; `containerPort` by itself does not configure nginx or forward a port.

```sh
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig apply -f examples/nginx.yaml --validate=false
curl http://127.0.0.1:8080
```

Run curl on the PocketKube host, or use `http://PHONE_IP:8080` from another machine. Networking is shared with the host, so port 8080 must be free. If replacing a failed Pod, delete it before applying the example again.

The runtime provides `/proc`, basic devices, and guest standard-stream links. Startup failures include the last 64 KiB of combined stdout/stderr in the Pod status message.

## Forward a Pod port

```sh
KUBECTL_PORT_FORWARD_WEBSOCKETS=true \
  kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig \
  port-forward pod/nginx 18080:8080
```

Then open `http://127.0.0.1:18080` on the machine running kubectl. Ctrl-C stops forwarding without stopping the Pod. Multiple ports and simultaneous TCP connections are supported. The Pod must be Running, and its application must actually listen on the target port; `containerPort` does not start a listener.

PocketKube supports kubectl's `SPDY/3.1+portforward.k8s.io` WebSocket tunnel (also accepted as `v2.portforward.k8s.io`). Use a kubectl version supporting WebSocket port forwarding, such as 1.31 or newer. Legacy direct HTTP SPDY upgrades and the Python client's older channel-based port-forward protocol are not supported.

PRoot uses the host network, so the target is `127.0.0.1:REMOTE_PORT` on the PocketKube device. This does not isolate ports between Pods. A Chisel reverse-forward listener on a remote server is not a listener on the phone and cannot be reached by forwarding the same phone port.

The Docker backend connects to the container IP (or host loopback for host-network containers), which requires a local Docker daemon with a container network reachable from PocketKube, typically native Linux. Remote Docker daemons and Docker Desktop VM networks are not guaranteed to be reachable.

Each tunnel allows up to 64 concurrent TCP connections. Stream pairing times out after 30 seconds, TCP connection attempts after 10 seconds, and stalled forwarding writes are bounded. Sockets close on client disconnect or Pod deletion. UDP forwarding is not supported.

## View container logs

Read the main container's combined stdout/stderr:

```sh
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig logs nginx
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig logs -f nginx --tail=20 --timestamps
```

Use Ctrl-C to stop following; the Pod keeps running. Requests to nginx on port 8080 generate access logs. Output from `kubectl exec` is separate from the main container's logs.

Additional supported options include `-c CONTAINER`, `--since=5m`, `--since-time=2026-09-15T00:00:00Z`, and `--limit-bytes=4096`. Use either `--since` or `--since-time`. `--tail=0 -f` follows only new output.

To try a continuously logging Pod:

```sh
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig apply -f examples/logs.yaml --validate=false
kubectl --kubeconfig ~/.kube/pocketkube.kubeconfig logs -f logs-demo
```

PRoot retains recent logs in memory, capped at 1 MiB or 16,384 records per Pod (whichever is reached first). A record is a line or a fragment of a long/partially written line. Older output is discarded; very slow followers can miss discarded output. Timestamps record when PocketKube receives each line. Failed-start logs remain readable until Pod deletion. Deleting a Pod clears its logs and ends followers; server restarts lose PRoot log history. Restart PocketKube and recreate existing Pods after upgrading to enable capture.

The Docker backend reads the Docker daemon's retained logs. `--previous` is unsupported because PocketKube does not manage container restarts. Logging covers stdout/stderr, not arbitrary files inside the container.

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
pocketkube serve --runtime proot
```

For desktop development with Docker:

```sh
pocketkube serve --runtime docker
```

Environment variables are also supported:

```sh
export POCKETKUBE_RUNTIME=proot
export POCKETKUBE_IMAGE_DIR=$HOME/.pocketkube/images
pocketkube serve
```

## Current raw PRoot semantics

A Pod runs in its own filesystem copy through PRoot. `kubectl exec` starts another PRoot process against that same copy. There are no real container namespaces or cgroups, and this is not a security boundary.

For legacy offline Alpine operation, explicitly pass `--rootfs ~/rootfs/alpine` or set `POCKETKUBE_PROOT_ROOTFS`. In this mode `alpine` / `alpine:*` use that directory as the source and ignore the requested tag; other images still use the registry. Omit this option to pull the actual Alpine tag.

## Supported API subset

Core `v1`:

- Nodes: GET/LIST and PATCH metadata.labels (cluster-scoped)
- Namespaces: GET/LIST/CREATE/DELETE
- Pods: GET/LIST/CREATE/DELETE
- Pods log: GET, including streaming follow
- Pods portforward: WebSocket TCP tunneling
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

- security isolation between Pods
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
- attach
- init containers or multiple containers per Pod
- authentication, authorization, TLS, admission control
- persistence across PocketKube server restarts

## Recommended next steps

1. Private registry authentication and additional layer formats.
2. Reduce the disk overhead of full per-Pod filesystem copies.
3. Persistent SQLite state.
4. Deployment reconciliation loop.
5. Persistent log storage and configurable rotation.
6. Additional terminal compatibility testing on legacy Android devices.
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

`kubectl exec` uses the Kubernetes WebSocket remote-command protocol. With `-t`, PocketKube allocates a controlling PTY and forwards resize events; terminal stderr shares stdout. Without `-t`, stdin/stdout/stderr remain separate pipes. The host must support PTY allocation for interactive sessions.

On Android, the interactive PTY helper launches Termux ELF executables through `/system/bin/linker` or `linker64` when available. This avoids direct-exec restrictions on app-data binaries without passing Termux preload libraries into the guest. The linker is selected from the executable’s ELF class, so both 32-bit and 64-bit Termux are supported.
