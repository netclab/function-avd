"""Crossplane composite function for the AVD living model.

One function serves two composite kinds (dispatched on ``kind``):

* ``Fabric`` -- runs the fabric-wide pyavd pipeline
  (``engine.render_fabric_design``) and emits one ``Device`` XR per host,
  carrying that device's structured config inline in ``spec.structuredConfig``.
* ``Device`` -- validates and renders its own structured config (pyavd
  ``validate_structured_config`` + ``get_device_config``), reports per-device
  status/conditions, and emits a ConfigMap artifact (structured config + EOS
  CLI). With ``spec.push`` set it also composes a provider-http ``Request``
  that keeps the device's running config in sync over eAPI (see push.py).

The gRPC entrypoint lives in ``main.py`` (function-template-python layout).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

import pyavd
import yaml
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

from . import pools, push
from .engine import (
    InputValidationError,
    device_roles_from_design,
    hostnames_from_design,
    render_structured_configs,
)
from .kinds import (
    KINDS,
    Input,
    hosts_in_blocks,
    overwrites,
    resolve,
    unmatched_patterns,
)

API_VERSION = "avd.netclab.dev/v1alpha1"


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


def _dns_name(hostname: str) -> str:
    """A hostname as a Kubernetes object name may spell it.

    AVD hostnames are free text and two of its eight bundled examples --
    `campus-fabric` and `l2ls-fabric` -- write them entirely in capitals. A
    composed resource named after one is rejected outright: *"invalid name
    ... Must be a valid RFC 1123 subdomain name"*, so the whole fabric fails to
    compose. The hostname itself is untouched; only the object's name is spelled
    this way.
    """
    return re.sub(r"[^a-z0-9-]+", "-", hostname.lower()).strip("-") or "device"


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
            self._reconcile_device(req, rsp, observed)
        elif kind in KINDS:
            self._reconcile_input(rsp, observed)
        else:
            response.fatal(rsp, f"unsupported composite kind: {kind!r}")
        return rsp

    # -- Inputs: a fragment of the design; they compose nothing ---------------

    def _reconcile_input(self, rsp: fnv1.RunFunctionResponse, observed: dict) -> None:
        """Report what this fragment contributes, on its own object.

        An input composes nothing -- a Fabric collects it. This reconcile exists
        so the team that owns the object sees its own shape here rather than
        buried in someone else's Fabric status.

        It cannot report `status.devices`: an input does not know the fabric's
        device list, so `appliesTo` only resolves where the inputs are collected.
        The Fabric fills that in. Validation is deliberately not attempted
        either -- whether pyavd can validate a fragment standalone is unsettled,
        and a green validation that never ran is worse than none.
        """
        spec = observed.get("spec") or {}
        design = spec.get("design") or {}
        status: dict = {"keys": sorted(design)}

        if observed.get("kind") == "NodeSet":
            declared = spec.get("declares")
            devices = set(declared) if declared is not None else hosts_in_blocks(design)
            status["deviceCount"] = len(devices)

        resource.update_status(rsp.desired.composite, status)
        response.normal(
            rsp, f"{observed.get('kind')} contributes {len(design)} top-level key(s)"
        )

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

        requires = spec.get("requires") or []
        if not fabric_name or not (design or requires):
            response.fatal(rsp, "spec.fabricName and spec.design or spec.requires are required")
            return

        if requires:
            all_inputs = self._collect(req, rsp, observed, requires, design, fabric_name)
            if all_inputs is None:
                return  # gated -- _collect reported why
        else:
            # The released path: one fabric-wide document handed to every device,
            # with AVD resolving roles from the node-type blocks.
            document = dict(design)
            document["fabric_name"] = fabric_name
            all_inputs = {host: document for host in hostnames_from_design(document)}

        # A fabric that asks for pool-assigned node IDs needs its assignments
        # back before it renders, or every device is renumbered on every pass.
        keeps_a_pool = pools.wanted_by(all_inputs)
        pool = ""
        try:
            if keeps_a_pool:
                previous = pools.observed_pool(req.observed.resources)
                if not previous.strip():
                    # Only before this fabric has a pool of its own. Once it has,
                    # the seed is history and must not override it.
                    state, seeded = pools.seed(req, rsp, spec, namespace)
                    if state == "pending":
                        # The normal first pass, exactly as for the named inputs.
                        response.set_conditions(
                            rsp,
                            resource.Condition(
                                typ="InputsResolved",
                                status="False",
                                reason="WaitingForSeed",
                                message="waiting for the node-ID pool seed ConfigMap",
                            ),
                        )
                        response.normal(rsp, "waiting for the node-ID pool seed")
                        return
                    if state == "missing":
                        response.fatal(
                            rsp,
                            "spec.nodeIdPool.seedConfigMapName names a ConfigMap that "
                            "does not exist; rendering without it would renumber every "
                            "device",
                        )
                        return
                    previous = seeded
                with pools.pool_manager(all_inputs, previous) as (manager, pool_file):
                    structured_configs = render_structured_configs(
                        all_inputs, pool_manager=manager
                    )
                    manager.save_updated_pools()
                    pool = pool_file.read_text() if pool_file.is_file() else previous
            else:
                structured_configs = render_structured_configs(all_inputs)
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

        if keeps_a_pool:
            # Composed after the render, so a failed render never overwrites a
            # good pool with a partial one.
            resource.update(
                rsp.desired.resources[pools.RESOURCE_NAME],
                pools.configmap(xr_name, namespace, fabric_name, pool),
            )
            rsp.desired.resources[pools.RESOURCE_NAME].ready = fnv1.READY_TRUE

        push_spec = spec.get("push") or {}
        if push_spec and not push_spec.get("credentialsSecretName"):
            response.fatal(rsp, "spec.push.credentialsSecretName is required when push is set")
            return
        # Only hosts that actually run somewhere get a push; the lab is a subset
        # of the fabric (LAB_HOSTS), and a Request against a non-existent Service
        # would sit Synced=False forever.
        push_hosts = set(push_spec.get("hosts") or structured_configs)
        url_template = push_spec.get(
            "urlTemplate", "https://{hostname}.{namespace}.svc/command-api"
        )

        # Roles come from each device's own view: with inputs, a leaf in DC1 sees
        # DC1's node-type block and nothing of DC2's.
        roles = {
            host: device_roles_from_design(hostvars).get(host) or hostvars.get("type", "")
            for host, hostvars in all_inputs.items()
        }
        # Two hostnames that differ only in case, or only in a character a
        # Kubernetes name cannot carry, would compose a single Device between
        # them -- one config silently standing in for two switches.
        spelled: dict[str, str] = {}
        for hostname in sorted(structured_configs):
            clash = spelled.setdefault(_dns_name(hostname), hostname)
            if clash != hostname:
                response.fatal(
                    rsp,
                    f"devices {clash!r} and {hostname!r} both need the object name "
                    f"{_dns_name(hostname)!r}; rename one",
                )
                return

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
                    "name": resource.child_name(xr_name, _dns_name(hostname)),
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
            if push_spec and hostname in push_hosts:
                device["spec"]["push"] = {
                    "url": url_template.format(hostname=hostname, namespace=namespace),
                    "credentialsSecretName": push_spec["credentialsSecretName"],
                    "providerConfigName": push_spec.get("providerConfigName", "eapi"),
                    "insecureSkipTLSVerify": push_spec.get("insecureSkipTLSVerify", True),
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

    # -- Collecting the inputs a Fabric names ---------------------------------

    def _collect(  # noqa: PLR0913
        self,
        req: fnv1.RunFunctionRequest,
        rsp: fnv1.RunFunctionResponse,
        observed: dict,
        requires: list[dict],
        design: dict,
        fabric_name: str,
    ) -> dict[str, dict] | None:
        """Ask Crossplane for the named inputs; layer them once they arrive.

        Returns per-device inputs, or ``None`` when the fabric must not render --
        the gate. That gate is not only a guard against a slow operator:
        requirements are answered on the *next* reconcile, so the first one
        always arrives with nothing at all, and rendering then would push a
        fabric short of its inputs as a full config replacement.
        """
        namespace = (observed.get("metadata") or {}).get("namespace", "default")

        # State the requirements on every reconcile. Crossplane fetches what the
        # latest response asked for, so leaving them out once drops the inputs.
        keys: list[tuple[str, dict]] = []
        for index, entry in enumerate(requires):
            kind = entry["kind"]
            key = f"{index:03d}-{kind.lower()}-{entry['name']}"
            keys.append((key, entry))
            response.require_resources(
                rsp,
                name=key,
                api_version="v1" if kind == "Secret" else API_VERSION,
                kind=kind,
                match_name=entry["name"],
                namespace=entry.get("namespace", namespace),
            )

        pending: list[str] = []
        absent: list[str] = []
        inputs: list[Input] = []
        for key, entry in keys:
            named = f"{entry['kind']}/{entry.get('namespace', namespace)}/{entry['name']}"
            if entry["kind"] == "Secret":
                # In the API from the first version so the mechanism can land
                # without a schema change, but not implemented. Refuse rather
                # than render a fabric whose credentials are silently absent.
                response.fatal(rsp, f"Secret inputs are not implemented yet: {named}")
                return None
            if key not in req.required_resources:
                pending.append(named)
                continue
            items = req.required_resources[key].items
            if not items:
                # Crossplane looked and found nothing. The proto distinguishes
                # this from "not fetched yet" by sending an empty Resources, and
                # that is what lets a Fabric tell "waiting" from "missing" --
                # the one thing this design was previously unable to do.
                absent.append(named)
                continue
            inputs.append(
                Input.from_xr(_normalize_numbers(resource.struct_to_dict(items[0].resource)))
            )

        if pending or absent:
            detail = []
            if absent:
                detail.append(f"not found: {', '.join(absent)}")
            if pending:
                detail.append(f"not fetched yet: {', '.join(pending)}")
            message = "; ".join(detail)
            response.set_conditions(
                rsp,
                resource.Condition(
                    typ="InputsResolved",
                    status="False",
                    reason="InputsMissing" if absent else "WaitingForInputs",
                    message=message[:400],
                ),
            )
            resource.update_status(
                rsp.desired.composite,
                {"fabricName": fabric_name, "validation": {"ok": False, "message": message}},
            )
            # Missing is a real problem; not-fetched-yet is the normal first pass.
            report = response.warning if absent else response.normal
            report(rsp, f"fabric {fabric_name} is waiting on inputs -- {message}")
            return None

        # The Fabric's own design is the first input: fabric-wide, seen by every
        # device, and declaring whatever devices its own blocks name so a Fabric
        # that carries both a design and a requires list still has its devices.
        document = dict(design)
        document["fabric_name"] = fabric_name
        inputs.insert(
            0,
            Input(
                name="fabric",
                kind="SettingSet",
                design=document,
                all_devices=True,
                declares=sorted(hosts_in_blocks(document)),
            ),
        )

        if stray := unmatched_patterns(inputs):
            listed = ", ".join(f"{name}: {pattern!r}" for name, pattern in stray)
            response.fatal(
                rsp,
                f"appliesTo.matchHostnames matched no device ({listed}) -- "
                f"a pattern that matches nothing is silent, so it is refused",
            )
            return None

        if replaced := overwrites(inputs):
            shown = ", ".join(f"{key} on {host} ({first} -> {second})"
                              for host, key, first, second in replaced[:5])
            response.warning(
                rsp,
                f"{len(replaced)} value(s) replaced by a later input: {shown}"
                + (" ..." if len(replaced) > 5 else ""),
            )

        response.set_conditions(
            rsp,
            resource.Condition(
                typ="InputsResolved",
                status="True",
                reason="AllInputsResolved",
                message=f"{len(inputs)} input(s)",
            ),
        )
        return resolve(inputs)

    # -- Device: validate + render one device's config -----------------------

    def _reconcile_device(
        self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, observed: dict
    ) -> None:
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

        conditions = [
            resource.Condition(typ="Validated", status="True", reason="SchemaValid"),
            resource.Condition(typ="Rendered", status="True", reason="ConfigRendered"),
        ]
        status = {
            "observedGeneration": generation,
            "nodeType": node_type,
            "configHash": config_hash,
            "managementAddress": _management_address(structured_config),
            "renderedBytes": len(eos_cli),
            "lastRenderedTime": last_rendered,
        }

        push_spec = spec.get("push") or {}
        if push_spec:
            self._reconcile_push(
                req, rsp, push_spec, prev, configmap["metadata"]["labels"],
                xr_name=xr_name, namespace=namespace, eos_cli=eos_cli,
                config_hash=config_hash, conditions=conditions, status=status,
            )

        response.set_conditions(rsp, *conditions)
        resource.update_status(rsp.desired.composite, status)
        response.normal(rsp, f"Validated and rendered {hostname} ({len(eos_cli)} bytes)")

    def _reconcile_push(
        self, req, rsp, push_spec, prev, labels, *,
        xr_name, namespace, eos_cli, config_hash, conditions, status,
    ) -> None:
        """Compose the eAPI push Request and fold its observed state back in.

        The recorded digest is what OBSERVE compares against. A push response is
        always authoritative for it; an observe response only fills the bootstrap
        window (no digest recorded yet for this configHash) -- accepting it later
        would bless manual drift as the new golden state (see push.py).
        """
        prev_push = prev.get("push") or {}
        recorded = prev_push.get("digest") if prev_push.get("configHash") == config_hash else None

        observed_request = (req.observed.resources or {}).get("push")
        if observed_request is not None:
            found = push.deployed_digest_from_observed(
                resource.struct_to_dict(observed_request.resource), config_hash
            )
            if found is not None:
                digest, source = found
                if source == "push" or recorded is None:
                    recorded = digest

        request = push.request_object(
            name=resource.child_name(xr_name, "push"),
            namespace=namespace,
            labels=labels,
            url=push_spec["url"],
            credentials_secret=push_spec["credentialsSecretName"],
            provider_config=push_spec.get("providerConfigName", "eapi"),
            insecure_skip_tls_verify=push_spec.get("insecureSkipTLSVerify", True),
            eos_cli=eos_cli,
            config_hash=config_hash,
            deployed_digest=recorded,
        )
        resource.update(rsp.desired.resources["push"], request)

        if recorded:
            # Same idempotency rule as lastRenderedTime: bump only on change.
            unchanged = (
                prev_push.get("configHash") == config_hash and prev_push.get("digest") == recorded
            )
            last_deployed = prev_push.get("lastDeployedTime") if unchanged else _now()
            rsp.desired.resources["push"].ready = fnv1.READY_TRUE
            conditions.append(
                resource.Condition(typ="Deployed", status="True", reason="ConfigDeployed")
            )
            status["push"] = {
                "configHash": config_hash,
                "digest": recorded,
                "lastDeployedTime": last_deployed,
            }
            status["lastDeployedTime"] = last_deployed
        else:
            conditions.append(
                resource.Condition(
                    typ="Deployed",
                    status="False",
                    reason="AwaitingPush",
                    message="config push not yet confirmed by the device",
                )
            )
            status["push"] = {"configHash": config_hash}
