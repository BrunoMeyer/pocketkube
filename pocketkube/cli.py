from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn


def kubeconfig(server: str) -> str:
    return f"""apiVersion: v1
kind: Config
clusters:
- name: pocketkube
  cluster:
    server: {server}
    insecure-skip-tls-verify: true
contexts:
- name: pocketkube
  context:
    cluster: pocketkube
    user: pocketkube
    namespace: default
current-context: pocketkube
users:
- name: pocketkube
  user: {{}}
"""


def main() -> None:
    p = argparse.ArgumentParser(prog="pocketkube")
    sub = p.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="start the tiny Kubernetes API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8443)
    serve.add_argument("--runtime", choices=["proot", "docker"], default=os.environ.get("POCKETKUBE_RUNTIME", "proot"))
    serve.add_argument(
        "--rootfs",
        default=os.environ.get("POCKETKUBE_PROOT_ROOTFS", str(Path.home() / "rootfs" / "alpine")),
        help="Alpine rootfs used by the raw PRoot runtime (default: ~/rootfs/alpine)",
    )

    cfg = sub.add_parser("kubeconfig", help="write a kubeconfig for kubectl")
    cfg.add_argument("--server", default="http://127.0.0.1:8443")
    cfg.add_argument("--output", default="./pocketkube.kubeconfig")

    args = p.parse_args()
    if args.cmd == "serve":
        os.environ["POCKETKUBE_RUNTIME"] = args.runtime
        if args.runtime == "proot":
            os.environ["POCKETKUBE_PROOT_ROOTFS"] = args.rootfs
        uvicorn.run("pocketkube.api:create_app", factory=True, host=args.host, port=args.port, log_level="info", ws="websockets")
    else:
        Path(args.output).write_text(kubeconfig(args.server))
        print(args.output)


if __name__ == "__main__":
    main()
