"""The provider-http Request that keeps a device's running config on its eos.cfg.

OBSERVE asks the device for two things, `expectedResponseCheck` compares them, and a
mismatch makes provider-http run the UPDATE mapping: one `configure session` that
replaces the whole configuration.

Two identities, because neither alone is enough:

* the **marker** -- `alias avd_cfg_<revision>`, pushed with the config -- names the
  eos.cfg the device runs. A new eos.cfg is a new revision, which is what makes a push.
* the **digest** -- `show running-config digest` -- is EOS's own hash of the running
  config. EOS reformats what it is given, so the digest cannot be computed from eos.cfg:
  it is read from the device right after a push, and every OBSERVE compares it. An edit
  made on the device changes it, and the next UPDATE takes the edit back.

Measured on cEOS for single-line commands. The multi-line form, `{"cmd", "input"}`, is
eAPI's own and has not been pushed to a device yet.
"""

from __future__ import annotations

import json

REQUEST_API_VERSION = "http.m.crossplane.io/v1alpha2"

# A command whose body follows on its own lines, up to a line reading EOF.
BLOCK_END = "EOF"


def revision(config_hash: str) -> str:
    """The hex of a `sha256:<hex>` configHash: the marker's and the push's identity."""
    return config_hash.split(":", 1)[-1]


def marker_line(config_hash: str) -> str:
    """The config line naming the revision the device runs.

    `show clock` is arbitrary: the alias exists to be found, never to be run.
    """
    return f"alias avd_cfg_{revision(config_hash)} show clock"


def _opens_block(stripped: str) -> bool:
    return stripped == "comment" or stripped.startswith("banner ")


def config_commands(eos_cli: str) -> list[str | dict]:
    """eos.cfg as the commands of one config session.

    Comment lines (`!`) and blank lines are dropped, and so is the closing `end`, which
    would leave config mode before the session commits. A `banner` or `comment` becomes
    one command with its lines as `input`, `!` lines included: inside the block they are
    text, not comments.
    """
    cmds: list[str | dict] = []
    lines = iter(eos_cli.splitlines())
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("!") or stripped == "end":
            continue
        if _opens_block(stripped):
            indent = len(line) - len(line.lstrip())
            body = []
            for inner in lines:
                if inner.strip() == BLOCK_END:
                    break
                body.append(inner[indent:] if inner[:indent].isspace() else inner.lstrip())
            else:
                raise ValueError(f"{stripped!r} has no {BLOCK_END} line")
            cmds.append({"cmd": line.rstrip(), "input": "\n".join(body)})
            continue
        cmds.append(line.rstrip())
    return cmds


def _eapi_body(cmds: list[str | dict], req_id: str, fmt: str = "json") -> str:
    """A JSON-RPC runCmds body. Literal JSON is a jq program too, which is what a
    mapping body is evaluated as."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "runCmds",
            "params": {"version": 1, "cmds": cmds, "format": fmt},
            "id": req_id,
        }
    )


def push_body(commands: list[str | dict], config_hash: str) -> str:
    """The CREATE and UPDATE body: one session replacing the whole configuration.

    The session has no name: EOS keeps one completed session in its history, so a fixed
    name works once, and every later push of the same revision -- the one taking an
    edit back -- fails as "already completed". The JSON-RPC id carries the revision
    instead, and `digest_from_observed` recognizes a push response by it. The closing
    `show running-config digest` makes the response carry the digest of the
    configuration just committed.
    """
    cmds = [
        "enable",
        "configure session",
        "rollback clean-config",
        *commands,
        marker_line(config_hash),
        "commit",
        "show running-config digest",
    ]
    return _eapi_body(cmds, f"push-{revision(config_hash)}")


def observe_body(config_hash: str) -> str:
    """The OBSERVE body: the marker and the digest, two reads.

    As text, because `| include` has no JSON form.
    """
    cmds = [
        "enable",
        "show running-config | include alias avd_cfg_",
        "show running-config digest",
    ]
    return _eapi_body(cmds, f"observe-{revision(config_hash)}", fmt="text")


def expected_check_logic(config_hash: str, deployed_digest: str | None) -> str:
    """The jq deciding, from an OBSERVE response, whether the device is in sync.

    Another marker, or an eAPI error, means another revision runs: push. With a digest
    recorded, the running config must also hash to what the last push left, so an edit
    on the device is pushed over. Before a digest is recorded the marker alone counts:
    the push has happened and its response is not read yet, and pushing again in that
    window only repeats it.
    """
    marker = f'if (.response.body.result[1].output | contains("avd_cfg_{revision(config_hash)} "))'
    if deployed_digest is None:
        return f"{marker} then true else false end"
    return (
        f"{marker}"
        f' and (.response.body.result[2].output | rtrimstr("\\n")) == "{deployed_digest}"'
        " then true else false end"
    )


def _response(observed_request: dict) -> tuple[dict, str]:
    """The Request's last response body, and the body that was sent for it."""
    status = observed_request.get("status") or {}
    try:
        body = json.loads((status.get("response") or {}).get("body") or "")
    except (TypeError, ValueError):
        return {}, ""
    sent = (status.get("requestDetails") or {}).get("body") or ""
    return (body if isinstance(body, dict) else {}), sent


def digest_from_observed(observed_request: dict, config_hash: str) -> tuple[str, str] | None:
    """`(digest, source)` for this revision, when the Request's last response holds one.

    A response is trusted only when it belongs to this configHash -- a status that lags
    would otherwise record another revision's digest, and the new eos.cfg would never
    be pushed:

    * `push` -- the response to this revision's session, recognized by the JSON-RPC id
      in `status.requestDetails.body`: the digest of the configuration just committed.
    * `observe` -- vouched for by the marker alone, which an edit on the device keeps.
      The caller takes it only while no digest is recorded for this revision, or an
      edited running config would be recorded as the one to keep.
    """
    body, sent = _response(observed_request)
    result = body.get("result") or []
    if not result or not isinstance(result[-1], dict):
        return None
    rev = revision(config_hash)
    last = result[-1]

    if f'"push-{rev}"' in sent:
        digest = last.get("digest")
        return (digest, "push") if digest else None

    if len(result) >= 3 and f"avd_cfg_{rev} " in (result[1].get("output") or ""):
        digest = (last.get("output") or "").strip()
        return (digest, "observe") if digest else None

    return None


def error_from_observed(observed_request: dict, config_hash: str) -> str | None:
    """eAPI's error for this revision's push, "" when it succeeded, None when unknown.

    provider-http reports a push that eAPI refuses as a success -- the error is only in
    the response body. Only a push response of this revision says anything: an OBSERVE
    between two pushes answers without an error while the push keeps failing.
    """
    body, sent = _response(observed_request)
    if f'"push-{revision(config_hash)}"' not in sent or not body:
        return None
    error = body.get("error")
    if not error:
        return ""
    said = [error.get("message") or f"eAPI error {error.get('code')}"]
    for item in error.get("data") or []:
        if isinstance(item, dict):
            said += [str(e) for e in item.get("errors") or []]
    return ": ".join(said)


def request_object(
    *,
    name: str,
    namespace: str,
    url: str,
    secret_name: str,
    secret_key: str,
    insecure_skip_tls_verify: bool,
    commands: list[str | dict],
    config_hash: str,
    deployed_digest: str | None,
) -> dict:
    """The Request pushing a Device's eos.cfg, as `config_commands` splits it.

    CREATE and UPDATE are the same replace, so a device is never half-configured. No
    REMOVE mapping, and no `Delete` in managementPolicies: deleting a Device stops
    managing the device and leaves its configuration where it is. No providerConfigRef:
    the default ClusterProviderConfig serves every namespace, and the credentials come
    with each Request.
    """
    body = push_body(commands, config_hash)
    headers = {
        "Content-Type": ["application/json"],
        # provider-http replaces {{ name:namespace:key }} with that Secret's key, which
        # holds base64 of user:password.
        "Authorization": [f"Basic {{{{ {secret_name}:{namespace}:{secret_key} }}}}"],
    }
    return {
        "apiVersion": REQUEST_API_VERSION,
        "kind": "Request",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "managementPolicies": ["Observe", "Create", "Update"],
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
