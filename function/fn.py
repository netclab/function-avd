"""The composite function: one runner for every avd.netclab.dev kind.

The kind of the observed composite decides what runs. Each kind's decisions live in its
own module and see plain dicts; this module moves them in and out of the request.
"""

from __future__ import annotations

import asyncio

from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

from . import device, fabric
from .structs import Composed, numbers, unchanged


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    def __init__(self) -> None:
        self.log = logging.get_logger()

    async def RunFunction(self, req: fnv1.RunFunctionRequest, _context) -> fnv1.RunFunctionResponse:
        rsp = response.to(req)
        composite = numbers(resource.struct_to_dict(req.observed.composite.resource))
        observed = {
            key: numbers(resource.struct_to_dict(res.resource))
            for key, res in req.observed.resources.items()
        }
        kind = composite.get("kind")
        if kind == "Device":
            self._device(rsp, composite, observed)
        elif kind == "Fabric":
            await self._fabric(req, rsp, composite, observed)
        else:
            response.fatal(rsp, f"no reconcile for kind {kind!r}")
        return rsp

    def _device(self, rsp: fnv1.RunFunctionResponse, composite: dict, observed: dict) -> None:
        try:
            composed = device.compose(composite, observed)
        except Exception as err:  # noqa: BLE001 -- pyavd's, reported on the Device
            # Fatal: Crossplane then changes nothing it composed.
            response.fatal(rsp, f"eos.cfg did not render: {type(err).__name__}: {err}")
            return
        _write(rsp, composed)

    async def _fabric(
        self,
        req: fnv1.RunFunctionRequest,
        rsp: fnv1.RunFunctionResponse,
        composite: dict,
        observed: dict,
    ) -> None:
        wanted = fabric.requirements(composite)
        for key, selector in wanted.items():
            response.require_resources(rsp, key, **selector)
        if not all(key in req.required_resources for key in wanted):
            # Crossplane fetches them and calls again; until then nothing changes.
            _write(rsp, Composed(status={}, resources=unchanged(observed)), status=False)
            return
        required = {
            key: [numbers(doc) for doc in request.get_required_resources(req, key)]
            for key in wanted
        }
        # A render is seconds of Ansible: off the event loop, so other calls are served.
        composed = await asyncio.to_thread(
            fabric.compose, composite, required, observed, fabric.render
        )
        _write(rsp, composed)


def _write(rsp: fnv1.RunFunctionResponse, composed: Composed, status: bool = True) -> None:
    for key, desired in composed.resources.items():
        resource.update(rsp.desired.resources[key], desired.resource)
        rsp.desired.resources[key].ready = fnv1.READY_TRUE if desired.ready else fnv1.READY_FALSE
    if status:
        resource.update_status(rsp.desired.composite, composed.status)
