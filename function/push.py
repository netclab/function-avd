"""Build the eAPI config-push managed resource for a Device.

The push is a provider-http ``Request`` (namespaced, ``http.m.crossplane.io``)
composed by the Device function. Crossplane's own reconcile loop is the watch:
OBSERVE asks the device for its config digest, ``expectedResponseCheck``
compares it, and a mismatch makes the provider re-run the UPDATE mapping --
a full ``configure session`` + ``rollback clean-config`` replace over eAPI.

Two identities cooperate, because neither alone is enough:

* the **marker** -- ``alias avd_cfg_<hash>`` pushed with the config -- names the
  model revision (``configHash``) the device is running. It is how a model
  change forces a push, and how the function knows an observed digest belongs
  to the *current* revision and not to the one a lagging status still shows.
* the **digest** -- ``show running-config digest`` -- is EOS's own hash of the
  canonicalized running config. It cannot be predicted from ``eos.cfg`` (EOS
  reformats), so it is *recorded* from the device right after a push, then
  compared on every OBSERVE: any manual change on the box flips it.

Everything here is pure and offline-testable; the live wiring lives in
``fn.py``.
"""

from __future__ import annotations

import json

REQUEST_API_VERSION = "http.m.crossplane.io/v1alpha2"

# Multi-line EOS commands need eAPI's {"cmd":..., "input":...} form; nothing in
# the lab fabrics renders one. Fail loudly rather than push a broken session.
MULTILINE_PREFIXES = ("banner ",)


def revision(config_hash: str) -> str:
    """The bare hex of a ``sha256:<hex>`` configHash: session + marker identity."""
    return config_hash.split(":", 1)[-1]


def marker_line(config_hash: str) -> str:
    """The config line naming the model revision the device runs.

    ``show clock`` is arbitrary -- the alias exists to be found by
    ``show running-config | include``, not to be executed.
    """
    return f"alias avd_cfg_{revision(config_hash)} show clock"


def config_commands(eos_cli: str) -> list[str]:
    """Rendered ``eos.cfg`` -> the command list for a config session.

    Comment/separator lines (``!``) and blanks are CLI no-ops, dropped for
    payload size; the trailing ``end`` would leave config mode before the
    session commits, so it is dropped too.
    """
    cmds = []
    for line in eos_cli.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        if stripped.startswith(MULTILINE_PREFIXES):
            raise ValueError(f"multi-line command not supported over runCmds: {stripped!r}")
        if stripped == "end":
            continue
        cmds.append(line.rstrip())
    return cmds


def _eapi_body(cmds: list[str], req_id: str, fmt: str = "json") -> str:
    """A JSON-RPC runCmds body. Literal JSON is also a valid jq program, which
    is what a Request mapping body is evaluated as."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "runCmds",
            "params": {"version": 1, "cmds": cmds, "format": fmt},
            "id": req_id,
        }
    )


def push_body(eos_cli: str, config_hash: str) -> str:
    """The CREATE/UPDATE mapping body: one atomic full-replace session.

    The session is deliberately *unnamed*: EOS keeps exactly one completed
    session in history, so a fixed name works once and then every retry -- in
    particular the drift-reclaim re-push of the same revision -- fails with
    "already completed". The device picks a fresh name each attempt; the
    request is tied to its revision by the JSON-RPC ``id`` instead, which is
    how ``deployed_digest_from_observed`` recognizes a push response. The
    final ``show running-config digest`` makes that response carry the digest
    of the exact config just committed.
    """
    cmds = [
        "enable",
        "configure session",
        "rollback clean-config",
        *config_commands(eos_cli),
        marker_line(config_hash),
        "commit",
        "show running-config digest",
    ]
    return _eapi_body(cmds, f"push-{revision(config_hash)}")


def observe_body(config_hash: str) -> str:
    """The OBSERVE mapping body: marker + digest, two cheap read commands.

    ``format: text`` because ``| include`` pipes have no JSON rendering.
    """
    cmds = [
        "enable",
        "show running-config | include alias avd_cfg_",
        "show running-config digest",
    ]
    return _eapi_body(cmds, f"observe-{revision(config_hash)}", fmt="text")


def expected_check_logic(config_hash: str, deployed_digest: str | None) -> str:
    """The jq deciding, from an OBSERVE response, whether the device is in sync.

    Marker mismatch (or eAPI error) -> the device runs another model revision ->
    push. With a recorded digest the running config must also hash to exactly
    what the last push left behind, so any manual edit on the box re-pushes.
    Before the first digest is recorded, the marker alone counts as in-sync:
    the push already happened, the function just hasn't read its result yet --
    re-pushing in that window would only churn.
    """
    marker = f"if (.response.body.result[1].output | contains(\"avd_cfg_{revision(config_hash)} \"))"
    if deployed_digest is None:
        return f"{marker} then true else false end"
    return (
        f"{marker}"
        f' and (.response.body.result[2].output | rtrimstr("\\n")) == "{deployed_digest}"'
        " then true else false end"
    )


def deployed_digest_from_observed(
    observed_request: dict, config_hash: str
) -> tuple[str, str] | None:
    """Extract ``(digest, source)`` for *this* model revision, if present.

    Trust ``status.response`` only when it provably belongs to the current
    ``configHash`` -- a lagging status otherwise records a stale digest as
    golden and the model change would never land:

    * source ``"push"`` -- the response to our replace session, recognized by
      the revision-scoped JSON-RPC id in ``status.requestDetails.body``: the
      digest of the exact config just committed. Always authoritative.
    * source ``"observe"`` -- vouched for only by the marker line, which
      survives manual edits on the box. The caller must accept it solely when
      no digest is recorded yet for this revision (the bootstrap window),
      otherwise a drifted running config would be re-recorded as golden.
    """
    status = observed_request.get("status") or {}
    body = status.get("response", {}).get("body")
    if not body:
        return None
    try:
        result = json.loads(body).get("result") or []
    except (TypeError, ValueError):
        return None
    if not result:
        return None

    rev = revision(config_hash)
    last = result[-1]
    if not isinstance(last, dict):
        return None

    if f'"push-{rev}"' in (status.get("requestDetails", {}).get("body") or ""):
        digest = last.get("digest")
        return (digest, "push") if digest else None

    if len(result) >= 3 and f"avd_cfg_{rev} " in (result[1].get("output") or ""):
        digest = (last.get("output") or "").strip()
        return (digest, "observe") if digest else None

    return None


def request_object(
    *,
    name: str,
    namespace: str,
    labels: dict[str, str],
    url: str,
    credentials_secret: str,
    provider_config: str,
    insecure_skip_tls_verify: bool,
    eos_cli: str,
    config_hash: str,
    deployed_digest: str | None,
) -> dict:
    """The composed namespaced ``Request`` pushing this Device's config.

    CREATE and UPDATE are the same full replace -- a device is never half-made.
    No REMOVE mapping, and ``managementPolicies`` without ``Delete`` (the
    namespaced-MR spelling of orphaning): deleting a Device stops managing the
    box, it does not wipe it (an empty box is not a desired state).
    """
    body = push_body(eos_cli, config_hash)
    headers = {
        "Content-Type": ["application/json"],
        # provider-http resolves {{ name:namespace:key }} from the Secret; the
        # key holds the ready-made base64 basic-auth token.
        "Authorization": [f"Basic {{{{ {credentials_secret}:{namespace}:basic }}}}"],
    }
    return {
        "apiVersion": REQUEST_API_VERSION,
        "kind": "Request",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "managementPolicies": ["Observe", "Create", "Update"],
            "providerConfigRef": {"name": provider_config, "kind": "ProviderConfig"},
            "forProvider": {
                "insecureSkipTLSVerify": insecure_skip_tls_verify,
                "headers": headers,
                "payload": {"baseUrl": url},
                "mappings": [
                    {"action": "CREATE", "method": "POST", "url": ".payload.baseUrl", "body": body},
                    {"action": "UPDATE", "method": "POST", "url": ".payload.baseUrl", "body": body},
                    {
                        "action": "OBSERVE",
                        "method": "POST",
                        "url": ".payload.baseUrl",
                        "body": observe_body(config_hash),
                    },
                ],
                "expectedResponseCheck": {
                    "type": "CUSTOM",
                    "logic": expected_check_logic(config_hash, deployed_digest),
                },
            },
        },
    }
