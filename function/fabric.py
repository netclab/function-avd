"""A Fabric: the user's Ansible, rebuilt and rendered by AVD's own role, as Devices.

What the Fabric composes:

    <fabric>-<host>   Device, one per host eos_designs wrote a structured config for
    <fabric>-eapi     Secret, base64 of user:password for each Device's eAPI: the most
                      common pair as `default`, a key per host only where it differs
    <fabric>-pools    ConfigMap, the pool files as eos_designs rewrote them -- the one
                      emit applied, taken over under its own name

The Fabric renders only once every object it names is found, and only when what it
reads has changed since the last render (`status.rendered`). When it does not render,
or the render fails, what it composed stays as it is.

Everything decided here reads and returns dicts; `render` is the one part that runs
Ansible, and `compose` takes it as an argument, so the rest is tested without it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from netadopt.api import (
    API_VERSION,
    FABRIC_LABEL,
    ensure_collections,
    fit,
    reconstruct,
    render_extra_vars,
    render_play,
    resolve_ansible,
    rfc1123,
)

from .structs import Composed, Desired, ready, unchanged

EAPI = "eapi"
POOLS = "pools"
# The Secret key holding the pair most hosts share.
DEFAULT_KEY = "default"

# Annotated on each Device: the host as the inventory spells it.
HOST_ANNOTATION = "avd.netclab.dev/host"

# Where the image keeps the collections; unset, netadopt's own cache.
COLLECTIONS_ENV = "AVD_COLLECTIONS"

# Seconds. Crossplane gives a whole reconcile 120.
RENDER_TIMEOUT = 100

# Ansible writes one fatal: line per host, and a fabric holds hundreds.
ERRORS_SHOWN = 10

PLAYBOOK = "function-avd.yml"
PUSH_DIR_VAR = "function_avd_push_dir"

# Appended to the play after AVD's role: what the push needs, per host, as Ansible
# resolves it -- the address, the credentials, and httpapi's transport, with httpapi's
# own defaults.
PUSH_TASK = {
    "name": "function-avd -- what the push to this host needs",
    "ansible.builtin.copy": {
        "dest": f"{{{{ {PUSH_DIR_VAR} }}}}/{{{{ inventory_hostname }}}}.json",
        "content": (
            "{{ {'host': ansible_host | default(inventory_hostname),"
            " 'user': ansible_user | default(''),"
            " 'password': ansible_password | default(''),"
            " 'port': ansible_httpapi_port | default(''),"
            " 'ssl': ansible_httpapi_use_ssl | default(false) | bool,"
            " 'validate': ansible_httpapi_validate_certs | default(true) | bool}"
            " | to_json }}"
        ),
    },
}

_SECRET_KEY = re.compile(r"^[-._a-zA-Z0-9]+$")


@dataclass(frozen=True)
class Read:
    """What the Fabric names, as found in its namespace."""

    inputs: list[dict] = field(default_factory=list)
    config_maps: dict[str, dict] = field(default_factory=dict)
    vault_password: str | None = None
    status: dict = field(default_factory=dict)  # inputs, configMaps, missing, unlisted
    problem: str | None = None  # what stops a render besides a missing object

    @property
    def complete(self) -> bool:
        missing = self.status.get("missing") or {}
        return not missing.get("inputs") and not missing.get("configMaps")


@dataclass(frozen=True)
class Render:
    """What one render wrote, per host, or why it wrote nothing."""

    structured: dict[str, dict] = field(default_factory=dict)
    push: dict[str, dict] = field(default_factory=dict)
    pools: dict[tuple[str, str], str] = field(default_factory=dict)  # (ConfigMap, key)
    problem: str | None = None


def requirements(fabric: dict) -> dict[str, dict]:
    """The objects the Fabric reads, as Crossplane's required resources, by key."""
    name, namespace = _name(fabric), _namespace(fabric)
    spec = fabric.get("spec") or {}
    wanted = {
        "labelled": {
            "api_version": API_VERSION,
            "kind": "FabricInput",
            "match_labels": {FABRIC_LABEL: name},
            "namespace": namespace,
        }
    }
    for input_name in _listed(spec):
        wanted[f"input:{input_name}"] = _by_name(API_VERSION, "FabricInput", input_name, namespace)
    for cm in _config_map_names(spec):
        wanted[f"configmap:{cm}"] = _by_name("v1", "ConfigMap", cm, namespace)
    secret = _vault_ref(spec)
    if secret:
        wanted["vault"] = _by_name("v1", "Secret", secret["name"], namespace)
    return wanted


def gather(fabric: dict, required: dict[str, list[dict]]) -> Read:
    """What was found of what `requirements` asked for, and what was not."""
    spec = fabric.get("spec") or {}
    listed = _listed(spec)

    inputs, found_inputs, missing_inputs = [], [], []
    for input_name in listed:
        got = required.get(f"input:{input_name}") or []
        if got:
            inputs.append(got[0])
            found_inputs.append(
                {
                    "name": input_name,
                    "generation": (got[0].get("metadata") or {}).get("generation", 0),
                }
            )
        else:
            missing_inputs.append(input_name)

    config_maps, found_cms, missing_cms = {}, [], []
    for cm in _config_map_names(spec):
        got = required.get(f"configmap:{cm}") or []
        if not got:
            missing_cms.append({"name": cm})
            continue
        config_maps[cm] = got[0]
        found_cms.append(
            {
                "name": cm,
                "resourceVersion": (got[0].get("metadata") or {}).get("resourceVersion", ""),
            }
        )
        data = got[0].get("data") or {}
        for entry in _carried(spec):
            ref = entry.get("configMapRef") or {}
            if ref.get("name") == cm and ref.get("key") not in data:
                missing_cms.append({"name": cm, "key": ref.get("key")})

    unlisted = sorted(
        {
            (doc.get("metadata") or {}).get("name")
            for doc in required.get("labelled") or []
            if (doc.get("metadata") or {}).get("name") not in listed
        }
    )

    vault_password, problem = None, None
    secret = _vault_ref(spec)
    if secret:
        got = required.get("vault") or []
        value = ((got[0].get("data") or {}).get(secret["key"]) if got else None) or None
        if value is None:
            problem = f"no Secret {secret['name']} holding the vault password under {secret['key']}"
        else:
            vault_password = base64.b64decode(value).decode()

    status = {
        "inputs": found_inputs,
        "configMaps": found_cms,
        "missing": {"inputs": missing_inputs, "configMaps": missing_cms},
        "unlisted": unlisted,
    }
    return Read(
        inputs=inputs,
        config_maps=config_maps,
        vault_password=vault_password,
        status=status,
        problem=problem,
    )


def content_hash(fabric: dict, read: Read, pools: dict[tuple[str, str], str] | None = None) -> str:
    """A hash of everything a render reads -- with `pools`, as the render left them.

    Content only, never a resourceVersion: the Fabric rewrites its pool ConfigMap
    itself, and a version would change with every render and render again. Nor the
    `spec.crossplane` Crossplane writes into the Fabric and each FabricInput, both XRs.
    """
    config_maps = {name: dict(cm.get("data") or {}) for name, cm in read.config_maps.items()}
    for (cm, key), text in (pools or {}).items():
        config_maps.setdefault(cm, {})[key] = text
    whole = {
        "spec": _own(fabric),
        "inputs": {(doc.get("metadata") or {}).get("name"): _own(doc) for doc in read.inputs},
        "configMaps": config_maps,
        "vault": hashlib.sha256((read.vault_password or "").encode()).hexdigest(),
    }
    return "sha256:" + hashlib.sha256(json.dumps(whole, sort_keys=True).encode()).hexdigest()


def _own(xr: dict) -> dict:
    """An XR's spec without what Crossplane writes into it."""
    return {key: value for key, value in (xr.get("spec") or {}).items() if key != "crossplane"}


Renderer = Callable[[dict, Read], Render]


def compose(
    fabric: dict, required: dict[str, list[dict]], observed: dict[str, dict], renderer: Renderer
) -> Composed:
    """The resources and status of `fabric`, rendering with `renderer` when it has to."""
    prev = fabric.get("status") or {}
    read = gather(fabric, required)
    status = {**read.status, "error": prev.get("error", "")}
    if "rendered" in prev:
        status["rendered"] = prev["rendered"]

    if not read.complete:
        return Composed(status=status, resources=unchanged(observed))
    if read.problem:
        return Composed(status={**status, "error": read.problem}, resources=unchanged(observed))

    before = content_hash(fabric, read)
    if before == prev.get("rendered"):
        return Composed(status=status, resources=unchanged(observed))

    rendered = renderer(fabric, read)
    if rendered.problem:
        status |= {"error": rendered.problem, "rendered": before}
        return Composed(status=status, resources=unchanged(observed))

    resources, problem = _resources(fabric, read, rendered, observed)
    if problem:
        status |= {"error": problem, "rendered": before}
        return Composed(status=status, resources=unchanged(observed))
    status |= {"error": "", "rendered": content_hash(fabric, read, rendered.pools)}
    return Composed(status=status, resources=resources)


def device_name(fabric_name: str, host: str) -> str:
    """A Device's name, spelled as emit spells a FabricInput's."""
    return fit(rfc1123(f"{fabric_name}-{host}"))


def _resources(
    fabric: dict, read: Read, rendered: Render, observed: dict[str, dict]
) -> tuple[dict[str, Desired], str | None]:
    name, namespace = _name(fabric), _namespace(fabric)
    labels = {FABRIC_LABEL: name}

    lost = sorted(set(rendered.structured) - set(rendered.push))
    if lost:
        return {}, f"no push variables written for {', '.join(lost)}"

    names: dict[str, str] = {}
    for host in rendered.structured:
        device = device_name(name, host)
        if device in names:
            return {}, f"hosts {names[device]} and {host} would both be Device {device}"
        names[device] = host

    secret_data, keys, problem = _credentials(rendered.push)
    if problem:
        return {}, problem

    resources: dict[str, Desired] = {}
    for device, host in names.items():
        resources[device] = Desired(
            {
                "apiVersion": API_VERSION,
                "kind": "Device",
                "metadata": {
                    "name": device,
                    "namespace": namespace,
                    "labels": labels,
                    "annotations": {HOST_ANNOTATION: host},
                },
                "spec": {
                    "structuredConfig": rendered.structured[host],
                    "eapi": _eapi(rendered.push[host], f"{name}-{EAPI}", keys[host]),
                },
            },
            ready=ready(observed.get(device)),
        )

    resources[EAPI] = Desired(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"{name}-{EAPI}", "namespace": namespace, "labels": labels},
            "data": {key: _b64(value) for key, value in secret_data.items()},
        },
        ready=True,
    )

    pool_maps = sorted({cm for cm, _ in rendered.pools})
    if len(pool_maps) > 1:
        return {}, f"spec.pools names several ConfigMaps: {', '.join(pool_maps)}"
    if pool_maps:
        cm = pool_maps[0]
        data = dict((read.config_maps.get(cm) or {}).get("data") or {})
        data |= {key: text for (_, key), text in rendered.pools.items()}
        resources[POOLS] = Desired(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": cm, "namespace": namespace, "labels": labels},
                "data": data,
            },
            ready=True,
        )
    return resources, None


def _credentials(push: dict[str, dict]) -> tuple[dict[str, str], dict[str, str], str | None]:
    """The Secret's values, and each host's key in it, or why there is none."""
    pairs = {host: _b64(f"{v.get('user', '')}:{v.get('password', '')}") for host, v in push.items()}
    if not pairs:
        return {}, {}, None
    common = Counter(pairs.values()).most_common(1)[0][0]
    data, keys = {DEFAULT_KEY: common}, {}
    for host, pair in pairs.items():
        if pair == common:
            keys[host] = DEFAULT_KEY
            continue
        if host == DEFAULT_KEY:
            return {}, {}, f"host {host!r} holds other credentials, and its key is the shared one's"
        if not _SECRET_KEY.match(host):
            return {}, {}, f"host {host!r} holds other credentials and cannot be a Secret key"
        data[host] = pair
        keys[host] = host
    return data, keys, None


def _eapi(push: dict, secret: str, key: str) -> dict:
    ssl = bool(push.get("ssl"))
    port = push.get("port") or (443 if ssl else 80)
    return {
        "url": f"{'https' if ssl else 'http'}://{push.get('host')}:{port}/command-api",
        "insecureSkipTLSVerify": not push.get("validate", True),
        "secretRef": {"name": secret, "key": key},
    }


def render(fabric: dict, read: Read) -> Render:
    """Rebuild the repository from what `read` holds, and run AVD's role on it."""
    ansible = resolve_ansible()
    if not ansible.usable:
        return Render(problem=f"no Ansible to render with: {ansible.problem}")
    root = os.environ.get(COLLECTIONS_ENV)
    collections = ensure_collections(ansible, root=Path(root) if root else None)
    if not collections.usable:
        return Render(problem=collections.problem)

    spec = fabric.get("spec") or {}
    with tempfile.TemporaryDirectory(prefix="function-avd-") as tmp:
        work = Path(tmp)
        repo, out, push_dir = work / "repo", work / "structured", work / "push"
        documents = [fabric, *read.inputs, *read.config_maps.values()]
        rebuilt = reconstruct(documents, repo, fabric=_name(fabric))
        if not rebuilt.usable:
            return Render(problem=f"the repository did not rebuild: {rebuilt.problem}")
        push_dir.mkdir()

        play = render_play(spec.get("play") or {})
        play["tasks"] = [*play["tasks"], PUSH_TASK]
        (repo / PLAYBOOK).write_text(yaml.safe_dump([play], sort_keys=False), encoding="utf-8")

        env = {
            **os.environ,
            "ANSIBLE_COLLECTIONS_PATH": str(collections.path),
            "ANSIBLE_HOME": str(work / "ansible"),
            "ANSIBLE_NOCOLOR": "1",
            "HOME": str(work),
        }
        if read.vault_password is not None:
            vault = work / "vault"
            vault.write_text(read.vault_password, encoding="utf-8")
            vault.chmod(0o600)
            env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(vault)

        command = playbook_command(str(ansible.exe), str(rebuilt.inventory), spec, out, push_dir)
        try:
            done = subprocess.run(
                command,
                check=False,
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=RENDER_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return Render(problem=f"the render did not finish in {RENDER_TIMEOUT}s")
        except OSError as err:
            return Render(problem=f"{ansible.exe} could not be run: {err}")
        if done.returncode != 0:
            return Render(problem=_why(done))

        return _read_render(spec, repo, rebuilt.inventory, out, push_dir)


def playbook_command(exe: str, inventory: str, spec: dict, out: Path, push_dir: Path) -> list[str]:
    """ansible-playbook over the rebuilt repository, the Fabric's extra vars first.

    The render's own come last, so they outrank the Fabric's: a lab's `-e` cannot make
    the render reach for a device. The interpreter running Ansible runs its modules too:
    the image has no other.
    """
    command = [exe, PLAYBOOK, "-i", inventory]
    if spec.get("extraVars"):
        command += ["-e", json.dumps(spec["extraVars"])]
    ours = render_extra_vars(out) | {
        PUSH_DIR_VAR: str(push_dir),
        "ansible_python_interpreter": "{{ ansible_playbook_python }}",
    }
    return [*command, "-e", json.dumps(ours)]


def _read_render(
    spec: dict, repo: Path, inventory: str | None, out: Path, push_dir: Path
) -> Render:
    structured: dict[str, dict] = {}
    for file in sorted(out.glob("*.yml")):
        loaded = yaml.load(file.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
        if not isinstance(loaded, dict):
            return Render(problem=f"{file.stem}: its structured config is not a mapping")
        structured[file.stem] = loaded
    if not structured:
        return Render(problem="eos_designs wrote no structured config")

    push = {
        file.stem: json.loads(file.read_text(encoding="utf-8")) for file in push_dir.glob("*.json")
    }

    bases = {"inventory": (repo / (inventory or ".")).parent, "playbook": repo}
    pools: dict[tuple[str, str], str] = {}
    for entry in spec.get("pools") or []:
        ref = entry.get("configMapRef") or {}
        file = bases.get(entry.get("beside"), repo) / entry.get("path", "")
        if file.is_file():
            pools[(ref.get("name"), ref.get("key"))] = file.read_text(encoding="utf-8")
    return Render(structured=structured, push=push, pools=pools)


def _why(done: subprocess.CompletedProcess) -> str:
    """The lines Ansible failed on, or its exit code when it named none."""
    said = done.stdout + done.stderr
    errors = [line for line in said.splitlines() if "fatal:" in line or "ERROR" in line]
    if not errors:
        return f"ansible-playbook exit {done.returncode}"
    shown = errors[:ERRORS_SHOWN]
    if len(errors) > ERRORS_SHOWN:
        shown.append(f"... and {len(errors) - ERRORS_SHOWN} more")
    return "\n".join(shown)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _name(obj: dict) -> str:
    return (obj.get("metadata") or {})["name"]


def _namespace(obj: dict) -> str:
    return (obj.get("metadata") or {})["namespace"]


def _listed(spec: dict) -> list[str]:
    return [name for name in spec.get("inputs") or [] if isinstance(name, str)]


def _carried(spec: dict) -> list[dict]:
    return [
        entry
        for entry in [*(spec.get("pools") or []), *(spec.get("files") or [])]
        if isinstance(entry, dict)
    ]


def _config_map_names(spec: dict) -> list[str]:
    names = [(entry.get("configMapRef") or {}).get("name") for entry in _carried(spec)]
    return list(dict.fromkeys(name for name in names if name))


def _vault_ref(spec: dict) -> dict | None:
    return (spec.get("vaultPassword") or {}).get("secretRef")


def _by_name(api_version: str, kind: str, name: str, namespace: str) -> dict:
    return {"api_version": api_version, "kind": kind, "match_name": name, "namespace": namespace}
