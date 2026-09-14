# Changelog

## 0.2.4

- Fix raw PRoot guest PATH so commands such as `kubectl exec ... -- cat ...` resolve inside Alpine instead of inheriting Termux paths.
- Respect Kubernetes exec stream flags. With `tty=true`, stderr is merged onto stdout channel 1 instead of incorrectly sending channel 2.
- Add legacy `setup.py` packaging so old Termux/setuptools installations do not require PEP 517 or `bdist_wheel`.


## 0.2.3

- Fixed `kubectl exec` WebSocket negotiation by switching Uvicorn from the `wsproto` backend to the `websockets` backend.
- Explicitly depends on `websockets==13.0` (Python >= 3.8).
- Kubernetes remote-command WebSocket handshakes now return `Sec-WebSocket-Protocol: v5.channel.k8s.io`.
- Kept the HTTP POST `/exec` handler only as an explanatory fallback for clients that fall back to SPDY.

## 0.2.2

- Add explicit `wsproto==1.2.0` dependency for Uvicorn WebSocket support.
- Force Uvicorn to use the wsproto backend.
- Return an informative HTTP 426 for old SPDY-style exec requests instead of 404.
- Document `KUBECTL_REMOTE_COMMAND_WEBSOCKETS=true`.
- Clarify that PTY/`-t` is not yet implemented.


## 0.2.1

- Fixed Alpine rootfs setup validation on Android/Termux: `/bin/sh` is an absolute symlink to `/bin/busybox`, so host-side `test -e` could falsely report it as missing.
- Added explicit BusyBox validation after extraction.

## 0.2.0

- Removed the `proot-distro` runtime dependency.
- Added a direct raw `proot` runtime for legacy/rootless Termux.
- Preserves `$PREFIX/lib` in `LD_LIBRARY_PATH` while starting host PRoot, then clears it inside the Alpine guest.
- Added `--rootfs` / `POCKETKUBE_PROOT_ROOTFS` configuration.
- Added a BusyBox-tar Alpine rootfs setup helper for the legacy Android 5 Termux tar/symlink issue.
- Changed the default runtime from `proot-distro` to `proot`.
- Changed examples to Alpine-only images supported by the current raw PRoot backend.
- Removed the old `runtime/proot_distro.py` module.
- Added raw PRoot unit tests.
- Made `/version` report ARM vs ARM64 based on the host architecture instead of hardcoding ARM64.