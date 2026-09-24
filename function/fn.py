"""The composite function: one runner for every avd.netclab.dev kind.

The kind of the observed composite decides what runs. Each kind's decisions live in its
own module and see plain dicts; this module moves them in and out of the request.
"""

from __future__ import annotations

from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

from . import device
from .structs import numbers


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    def __init__(self) -> None:
        self.log = logging.get_logger()

    async def RunFunction(self, req: fnv1.RunFunctionRequest, _context) -> fnv1.RunFunctionResponse:
        rsp = response.to(req)
        composite = numbers(resource.struct_to_dict(req.observed.composite.resource))
        kind = composite.get("kind")
        if kind == "Device":
            self._device(req, rsp, composite)
        else:
            response.fatal(rsp, f"no reconcile for kind {kind!r}")
        return rsp

    def _device(
        self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, composite: dict
    ) -> None:
        observed = {
            key: numbers(resource.struct_to_dict(res.resource))
            for key, res in req.observed.resources.items()
        }
        try:
            composed = device.compose(composite, observed)
        except Exception as err:  # noqa: BLE001 -- pyavd's, reported on the Device
            # Fatal: Crossplane then changes nothing it composed.
            response.fatal(rsp, f"eos.cfg did not render: {type(err).__name__}: {err}")
            return
        for key, desired in composed.resources.items():
            resource.update(rsp.desired.resources[key], desired.resource)
            rsp.desired.resources[key].ready = (
                fnv1.READY_TRUE if desired.ready else fnv1.READY_FALSE
            )
        resource.update_status(rsp.desired.composite, composed.status)
