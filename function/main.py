"""Serve the composite function (function/fn.py) over gRPC.

The layout follows crossplane/function-template-python: ``main.py`` is the
entrypoint Crossplane runs, ``fn.py`` holds the FunctionRunner.

Run locally for ``crossplane render``:

    uv run avd-function --insecure --debug     # listens on :9443
"""

from __future__ import annotations

import argparse
import os

from crossplane.function import logging, runtime

from .fn import FunctionRunner

# Structured configs are large; raise gRPC message limits well above the 4 MiB
# default so big fabrics fit in one request/response.
_MAX_MSG = 64 * 1024 * 1024


def main() -> None:
    parser = argparse.ArgumentParser(description="AVD composite function (Fabric + Device)")
    parser.add_argument("--address", default="0.0.0.0:9443", help="listen address")
    parser.add_argument("--insecure", action="store_true", help="serve without TLS (dev only)")
    parser.add_argument(
        "--tls-certs-dir",
        default=os.getenv("TLS_SERVER_CERTS_DIR"),
        help="directory with tls.crt/tls.key/ca.crt; Crossplane sets TLS_SERVER_CERTS_DIR in-cluster",
    )
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args()

    logging.configure(level=logging.Level.DEBUG if args.debug else logging.Level.INFO)
    # In-cluster Crossplane mounts TLS certs and points TLS_SERVER_CERTS_DIR at
    # them -> serve securely. Locally (no certs) fall back to --insecure.
    creds = runtime.load_credentials(args.tls_certs_dir) if args.tls_certs_dir else None
    insecure = args.insecure or creds is None
    runtime.serve(
        FunctionRunner(),
        args.address,
        creds=creds,
        insecure=insecure,
        options=[
            ("grpc.max_receive_message_length", _MAX_MSG),
            ("grpc.max_send_message_length", _MAX_MSG),
        ],
    )


if __name__ == "__main__":
    main()
