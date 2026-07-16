"""Crossplane composite function for the AVD living model.

One function serves two composite kinds (dispatched on ``kind``):

* ``Fabric`` -- runs the fabric-wide pyavd pipeline
  (``engine.render_fabric_design``) and emits one ``Device`` XR per host,
  carrying that device's structured config inline in ``spec.structuredConfig``.
* ``Device`` -- validates and renders its own structured config (pyavd
  ``validate_structured_config`` + ``get_device_config``), reports per-device
  status/conditions, and emits a ConfigMap artifact (structured config + EOS
  CLI). The config push to the device/CVP is a managed resource added later.

Run locally for ``crossplane render``:

    uv run avd-function --insecure --debug     # listens on :9443
"""

from __future__ import annotations

import argparse
import hashlib
import os
from datetime import datetime, timezone

import pyavd
import yaml
from crossplane.function import logging, resource, response, runtime
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

from .engine import (
    InputValidationError,
    device_roles_from_design,
    render_fabric_design,
)

API_VERSION = "avd.netclab.dev/v1alpha1"

# Structured configs are large; raise gRPC message limits well above the 4 MiB
# default so big fabrics fit in one request/response.
_MAX_MSG = 64 * 1024 * 1024


def _normalize_numbers(obj):
    """Restore integers lost to protobuf Struct's double-only number type.

    Crossplane passes resources as protobuf ``Struct``, whose only numeric type
    is ``double`` -- so a VLAN id ``4092`` arrives as ``4092.0``. pyavd's schema
    expects ints for these fields, so we recursively coerce whole-number floats
    back to ``int`` (bools and genuine fractionals are left untouched). AVD
    up-converts int->float itself where a float is actually wanted.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return int(obj) if obj.is_integer() else obj
    if isinstance(obj, dict):
        return {k: _normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_numbers(v) for v in obj]
    return obj


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _observed_ready(observed_resource) -> bool:
    """True if an observed composed resource reports Ready=True."""
    if observed_resource is None:
        return False
    state = resource.struct_to_dict(observed_resource.resource)
    for cond in (state.get("status") or {}).get("conditions") or []:
        if cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def _management_address(structured_config: dict) -> str:
    for iface in structured_config.get("management_interfaces") or []:
        ip = iface.get("ip_address")
        if ip:
            return ip.split("/")[0]
    return ""


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """Serves RunFunction for both the Fabric and Device compositions."""

    def __init__(self) -> None:
        self.log = logging.get_logger()

    async def RunFunction(  # noqa: N802 (gRPC method name)
        self, req: fnv1.RunFunctionRequest, _context
    ) -> fnv1.RunFunctionResponse:
        rsp = response.to(req)
        observed = _normalize_numbers(resource.struct_to_dict(req.observed.composite.resource))
        kind = observed.get("kind")
        if kind == "Fabric":
            self._reconcile_fabric(req, rsp, observed)
        elif kind == "Device":
            self._reconcile_device(rsp, observed)
        else:
            response.fatal(rsp, f"unsupported composite kind: {kind!r}")
        return rsp

    # -- Fabric: fabric-wide model -> one Device XR per host ------------------

    def _reconcile_fabric(
        self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, observed: dict
    ) -> None:
        spec = observed.get("spec") or {}
        fabric_name = spec.get("fabricName")
        design = spec.get("design") or {}
        meta = observed.get("metadata") or {}
        namespace = meta.get("namespace", "default")
        xr_name = meta.get("name") or (fabric_name or "fabric").lower()

        if not fabric_name or not design:
            response.fatal(rsp, "spec.fabricName and spec.design are required")
            return

        try:
            structured_configs = render_fabric_design(design, fabric_name)
        except InputValidationError as err:
            resource.update_status(
                rsp.desired.composite,
                {"fabricName": fabric_name, "validation": {"ok": False, "message": str(err)}},
            )
            response.fatal(rsp, f"AVD input validation failed for fabric {fabric_name}")
            return
        except Exception as err:  # noqa: BLE001 - surface any AVD/render failure to the XR
            response.fatal(rsp, f"AVD render failed: {type(err).__name__}: {err}")
            return

        roles = device_roles_from_design(design)
        observed_devices = req.observed.resources  # keyed by composition-resource-name (hostname)
        devices = []
        for hostname, structured_config in structured_configs.items():
            node_type = roles.get(hostname, "")
            # Name scoped to the XR (not fabric_name) so two fabrics with the same
            # fabric_name / overlapping hostnames never collide over a Device.
            device = {
                "apiVersion": API_VERSION,
                "kind": "Device",
                "metadata": {
                    "name": resource.child_name(xr_name, hostname),
                    "namespace": namespace,
                    "labels": {
                        "avd.netclab.dev/fabric": fabric_name,
                        "avd.netclab.dev/fabric-xr": xr_name,
                        "avd.netclab.dev/device": hostname,
                        "avd.netclab.dev/node-type": node_type,
                    },
                },
                "spec": {
                    "hostname": hostname,
                    "fabricName": fabric_name,
                    "nodeType": node_type,
                    "structuredConfig": structured_config,
                },
            }
            resource.update(rsp.desired.resources[hostname], device)
            # With function pipelines Crossplane does NOT auto-derive a composed
            # resource's readiness from its Ready condition -- the function must
            # set it. Propagate the observed Device's real readiness up, so
            # Fabric.Ready means every device actually validated & rendered.
            if _observed_ready(observed_devices.get(hostname)):
                rsp.desired.resources[hostname].ready = fnv1.READY_TRUE
            devices.append({"hostname": hostname, "nodeType": node_type})

        resource.update_status(
            rsp.desired.composite,
            {
                "fabricName": fabric_name,
                "deviceCount": len(structured_configs),
                "devices": sorted(devices, key=lambda d: d["hostname"]),
                "validation": {"ok": True, "message": "rendered"},
            },
        )
        response.normal(
            rsp, f"Composed {len(structured_configs)} Device(s) for fabric {fabric_name}"
        )

    # -- Device: validate + render one device's config -----------------------

    def _reconcile_device(self, rsp: fnv1.RunFunctionResponse, observed: dict) -> None:
        spec = observed.get("spec") or {}
        hostname = spec.get("hostname")
        structured_config = spec.get("structuredConfig") or {}
        node_type = spec.get("nodeType", "")
        meta = observed.get("metadata") or {}
        namespace = meta.get("namespace", "default")
        xr_name = meta.get("name") or hostname or "device"
        generation = meta.get("generation")

        if not hostname or not structured_config:
            response.fatal(rsp, "spec.hostname and spec.structuredConfig are required")
            return

        # Validate the structured config against the AVD schema.
        violations = pyavd.validate_structured_config(structured_config).validation_result.violations
        if violations:
            response.set_conditions(
                rsp,
                resource.Condition(
                    typ="Validated",
                    status="False",
                    reason="SchemaViolations",
                    message=f"{len(violations)} violation(s); first: {violations[0]}",
                ),
            )
            response.fatal(rsp, f"structured config for {hostname} is invalid")
            return

        # Render EOS CLI from the structured config.
        try:
            eos_cli = pyavd.get_device_config(structured_config)
        except Exception as err:  # noqa: BLE001 - surface render failures on the Device
            response.set_conditions(
                rsp,
                resource.Condition(typ="Validated", status="True", reason="SchemaValid"),
                resource.Condition(
                    typ="Rendered",
                    status="False",
                    reason="RenderError",
                    message=f"{type(err).__name__}: {err}",
                ),
            )
            response.fatal(rsp, f"failed to render EOS CLI for {hostname}")
            return

        config_hash = "sha256:" + hashlib.sha256(eos_cli.encode()).hexdigest()[:16]

        # Idempotency: only bump the timestamp when the rendered config actually
        # changed. Emitting _now() every reconcile would make the status differ
        # each pass -> perpetual re-reconcile (a watch storm / circuit breaker).
        prev = observed.get("status") or {}
        last_rendered = prev.get("lastRenderedTime") if prev.get("configHash") == config_hash else _now()

        # Emit the rendered artifacts as a ConfigMap owned by this Device.
        configmap = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": resource.child_name(xr_name, "rendered"),
                "namespace": namespace,
                "labels": {
                    "avd.netclab.dev/fabric": spec.get("fabricName", ""),
                    "avd.netclab.dev/device": hostname,
                    "avd.netclab.dev/node-type": node_type,
                },
            },
            "data": {
                "structured_config.yaml": yaml.safe_dump(
                    structured_config, sort_keys=False, default_flow_style=False
                ),
                "eos.cfg": eos_cli,
            },
        }
        resource.update(rsp.desired.resources["rendered-config"], configmap)
        rsp.desired.resources["rendered-config"].ready = fnv1.READY_TRUE

        response.set_conditions(
            rsp,
            resource.Condition(typ="Validated", status="True", reason="SchemaValid"),
            resource.Condition(typ="Rendered", status="True", reason="ConfigRendered"),
        )
        resource.update_status(
            rsp.desired.composite,
            {
                "observedGeneration": generation,
                "nodeType": node_type,
                "configHash": config_hash,
                "managementAddress": _management_address(structured_config),
                "renderedBytes": len(eos_cli),
                "lastRenderedTime": last_rendered,
            },
        )
        response.normal(rsp, f"Validated and rendered {hostname} ({len(eos_cli)} bytes)")


def cli() -> None:
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
    cli()
