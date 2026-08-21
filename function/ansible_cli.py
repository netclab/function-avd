"""Ansible, asked rather than reimplemented.

Everything about an inventory that the migration needs is something Ansible
already states, so nothing here parses a ``group_vars`` tree, expands an
inventory pattern or opens a vault. Four questions, three subprocesses:

``ansible-inventory --list --export``
    Which group carries which variable -- the ownership boundary an input XR
    maps to. Inline ``vars:`` blocks, ``group_vars/<g>.yml``,
    ``group_vars/<g>/*.yml`` and their ``.yaml`` spellings are already merged
    into one namespace per group, exactly as one XR carries one fragment.

``ansible <all> -m debug -a var=hostvars[inventory_hostname]``
    The merged per-host variables **after templating**. Not a source -- an
    oracle. The migration layers the fragments itself and refuses to emit
    anything unless the result equals this, so its precedence model can never be
    silently wrong.

    An ad-hoc run rather than ``ansible-inventory --list`` because
    ``ansible-inventory`` does not template and has no flag that makes it:
    `{{ playbook_dir }}`, `{{ spine_bgp_defaults }}` and cv-pathfinder's
    `bgp_password | arista.avd.encrypt` come out as literal strings, and pyavd
    then meets a `Str` where it wants a `List`. A play templates; this is the
    cheapest thing that is a play. `--list` remains available for a caller that
    wants the raw text.

``ansible-playbook --list-tasks`` / ``--list-hosts``
    Which play runs eos_designs, and which devices it runs on. AVD renders a
    play, not an inventory, and Ansible resolves the host pattern with its own
    engine -- including ``!`` exclusions and ``:&`` intersections, which reading
    the playbook's ``hosts:`` string cannot do.

Only the *harness* depends on this. The runtime takes XRs and never sees an
inventory, so ``ansible-core`` is a development dependency and reaches neither
the image nor the published package.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ALL_GROUP = "all"
TIMEOUT = 900

#: Transport variables that make an ad-hoc `debug` fail although it never
#: connects. `ansible_connection` in the inventory outranks `-c`, and AVD's
#: examples set it to `ansible.netcommon.httpapi` with `become_method: enable`,
#: neither of which is installed here. Extra vars are the only precedence level
#: above inventory vars.
_ADHOC_OVERRIDES = ("ansible_connection=local", "ansible_become=false")

# `play #3 (FABRIC): Build Configurations	TAGS: []`
_PLAY = re.compile(r"^\s*play #(\d+) \((?P<pattern>.*)\): (?P<name>.*)\tTAGS:")
# `      arista.avd.eos_designs : Validate eos_designs inputs	TAGS: []`
_TASK = re.compile(r"^\s+(?P<role>[\w.]+) : .*\tTAGS:")


class AnsibleError(RuntimeError):
    """An ansible CLI call failed. Carries what it printed."""


@dataclass(frozen=True)
class Play:
    """One play, as ``ansible-playbook`` reports it."""

    playbook: str
    index: int
    name: str
    pattern: str
    hosts: tuple[str, ...]
    roles: frozenset[str]

    @property
    def runs_eos_designs(self) -> bool:
        return any(role.endswith("eos_designs") for role in self.roles)


@dataclass
class Inventory:
    """An inventory as Ansible describes it, at both levels."""

    group_vars: dict[str, dict] = field(default_factory=dict)
    host_vars: dict[str, dict] = field(default_factory=dict)
    children: dict[str, set[str]] = field(default_factory=dict)
    direct_hosts: dict[str, set[str]] = field(default_factory=dict)
    #: merged per-host variables -- the oracle, never a source
    hostvars: dict[str, dict] = field(default_factory=dict)
    #: whether `hostvars` had its Jinja evaluated
    templated: bool = False

    @property
    def depth(self) -> dict[str, int]:
        """Longest path from ``all``, which is what orders Ansible's groups.

        The one rule here that Ansible does not print. It is never trusted:
        :func:`function.migrate.layer` reproduces ``hostvars`` with it, and the
        migration refuses to emit XRs when that fails.
        """
        out = {g: 0 for g in self.children}
        changed = True
        while changed:
            changed = False
            for parent, kids in self.children.items():
                for kid in kids:
                    if out.get(parent, 0) + 1 > out.get(kid, 0):
                        out[kid] = out[parent] + 1
                        changed = True
        return out

    def members(self, group: str) -> set[str]:
        """Every host in the group, transitively."""
        seen: set[str] = set()
        stack = [group]
        hosts: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            hosts |= self.direct_hosts.get(current, set())
            stack.extend(self.children.get(current, ()))
        return hosts

    def groups_of(self, host: str) -> list[str]:
        """The host's groups, in Ansible's precedence order."""
        mine = {g for g in self.children if host in self.members(g)} | {ALL_GROUP}
        depth = self.depth
        return sorted(mine, key=lambda g: (depth.get(g, 0), g))

    @property
    def hosts(self) -> set[str]:
        return self.members(ALL_GROUP) | set(self.host_vars)


def _strip(data: dict) -> dict:
    """Drop Ansible's own transport variables -- not part of the AVD model."""
    return {k: v for k, v in data.items() if not k.startswith("ansible_")}


def _env(collections: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    if collections is not None:
        env["ANSIBLE_COLLECTIONS_PATH"] = str(collections)
    return env


def _run(binary: str, args: list[str], cwd: Path, collections: Path | None) -> str:
    """Run an ansible CLI in the inventory's own directory.

    The directory matters: ``ansible.cfg`` is read from the working directory,
    and it is what points cv-pathfinder at its vault password file.
    """
    path = shutil.which(binary)
    if path is None:
        raise AnsibleError(
            f"{binary} not found. This is a development dependency; install it "
            f"with `uv run --with ansible-core ...` or add it to the dev group."
        )
    result = subprocess.run(
        [path, *args], cwd=cwd, capture_output=True, text=True,
        env=_env(collections), timeout=TIMEOUT,
    )
    if result.returncode != 0:
        raise AnsibleError(
            f"{binary} {' '.join(args)} (in {cwd}) exited {result.returncode}:\n"
            f"{(result.stderr or result.stdout).strip()[:2000]}"
        )
    return result.stdout


def find_inventory(root: Path) -> Path:
    """The inventory file, in either layout AVD ships.

    Absolute, always. Every call runs with ``cwd`` set to the inventory's own
    directory, and a relative ``-i`` that does not resolve from there makes
    ``ansible-inventory`` **exit 0 with an empty inventory** rather than fail --
    a silent wrong answer, not an error.
    """
    root = Path(root).resolve()
    for candidate in (root / "inventory.yml", root / "inventory" / "hosts.yml"):
        if candidate.is_file():
            return candidate
    raise AnsibleError(f"no inventory.yml or inventory/hosts.yml under {root}")


def templated_hostvars(root: Path, inventory: Path, collections: Path | None,
                       known: set[str]) -> dict[str, dict]:
    """Per-host variables as a play sees them -- Jinja evaluated.

    ``--tree`` writes one JSON file per host, which is the only machine-readable
    output an ad-hoc run offers.

    ⚠ **A value source, never a key source.** The dump carries Ansible's magic
    variables (`groups`, `inventory_hostname`, `playbook_dir`, ...) beside the
    inventory's own, and those are not part of any document. Rather than name
    them -- a literal that would go stale the way every literal here has -- keep
    only the keys the inventory itself declares, which ``--export`` already said.
    """
    out: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tree:
        _run(
            "ansible",
            ["all", "-i", str(inventory), "-m", "ansible.builtin.debug",
             "-a", "var=hostvars[inventory_hostname]",
             *[arg for override in _ADHOC_OVERRIDES for arg in ("-e", override)],
             "--tree", tree],
            root, collections,
        )
        for path in sorted(Path(tree).iterdir()):
            try:
                body = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            data = body.get("hostvars[inventory_hostname]")
            if isinstance(data, dict):
                out[path.name] = {k: v for k, v in data.items() if k in known}
    return out


def read_inventory(root: Path, inventory: Path | None = None,
                   collections: Path | None = None, templated: bool = True) -> Inventory:
    """Both levels of the inventory, in two subprocesses.

    ``templated`` decides what the oracle is: what a play would hand AVD
    (default), or the raw text ``ansible-inventory --list`` prints. Two calls
    either way -- the templated oracle replaces ``--list`` rather than joining it.
    """
    root = Path(root).resolve()
    inventory = Path(inventory).resolve() if inventory else find_inventory(root)
    args = ["-i", str(inventory), "--playbook-dir", str(root), "--list"]
    export = json.loads(_run("ansible-inventory", [*args, "--export"], root, collections))
    merged = (
        {} if templated
        else json.loads(_run("ansible-inventory", args, root, collections))
    )

    inv = Inventory()
    for group, body in export.items():
        if group == "_meta":
            continue
        inv.group_vars[group] = _strip(body.get("vars") or {})
        inv.children[group] = set(body.get("children") or [])
        inv.direct_hosts[group] = set(body.get("hosts") or [])
    for group in list(inv.children):
        for kid in inv.children[group]:
            inv.children.setdefault(kid, set())
            inv.direct_hosts.setdefault(kid, set())
    inv.children.setdefault(ALL_GROUP, set())

    for host, data in (export.get("_meta", {}).get("hostvars") or {}).items():
        inv.host_vars[host] = _strip(data)
    if templated:
        known = {k for design in inv.group_vars.values() for k in design}
        known |= {k for design in inv.host_vars.values() for k in design}
        inv.hostvars = templated_hostvars(root, inventory, collections, known)
        inv.templated = True
    else:
        for host, data in (merged.get("_meta", {}).get("hostvars") or {}).items():
            inv.hostvars[host] = _strip(data)
    # A host carrying no variables at all is omitted by both; it still exists,
    # and its merged view is empty.
    for host in inv.hosts:
        inv.hostvars.setdefault(host, {})
    return inv


def _parse_plays(text: str, playbook: str) -> dict[int, dict]:
    """Split ``--list-hosts --list-tasks`` output into plays by index.

    The two flags combine, so one subprocess answers both questions: which
    devices a play targets, and which roles it runs. Asking separately cost
    twice as many processes and told us nothing more.
    """
    plays: dict[int, dict] = {}
    current: int | None = None
    section: str | None = None
    for line in text.splitlines():
        header = _PLAY.match(line)
        if header:
            current = int(header.group(1))
            section = None
            plays[current] = {
                "playbook": playbook,
                "name": header.group("name").strip(),
                "pattern": header.group("pattern"),
                "hosts": [],
                "tasks": [],
            }
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("hosts (") and stripped.endswith("):"):
            section = "hosts"
            continue
        if stripped == "tasks:":
            section = "tasks"
            continue
        if section == "hosts" and stripped and not stripped.endswith(":"):
            plays[current]["hosts"].append(stripped)
        elif section == "tasks":
            task = _TASK.match(line)
            if task:
                plays[current]["tasks"].append(task.group("role"))
    return plays


def plays(root: Path, collections: Path | None = None,
          inventory: Path | None = None) -> list[Play]:
    """Every play in every playbook beside the inventory, with hosts and roles.

    One subprocess per playbook. They can be passed together in a single call,
    but a playbook that does not resolve then takes every other one down with
    it -- AVD's examples ship `deploy.yml`, which needs the `arista.eos`
    collection -- so the batch is not worth the failure it introduces.

    Playbooks that do not parse are skipped rather than fatal: a molecule
    scenario keeps ``molecule.yml`` next to its playbooks and that is a config
    file, not a play.
    """
    root = Path(root).resolve()
    inventory = Path(inventory).resolve() if inventory else find_inventory(root)
    found: list[Play] = []
    for playbook in sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml")):
        if playbook.resolve() == inventory.resolve() or playbook.name == "molecule.yml":
            continue
        try:
            output = _run(
                "ansible-playbook",
                ["-i", str(inventory), playbook.name, "--list-hosts", "--list-tasks"],
                root, collections,
            )
        except AnsibleError:
            continue
        for index, play in sorted(_parse_plays(output, playbook.name).items()):
            found.append(Play(
                playbook=playbook.name,
                index=index,
                name=play["name"],
                pattern=play["pattern"],
                hosts=tuple(play["hosts"]),
                roles=frozenset(play["tasks"]),
            ))
    return found


def design_plays(root: Path, collections: Path | None = None,
                 inventory: Path | None = None) -> list[Play]:
    """The plays that run eos_designs -- one fabric each."""
    return [p for p in plays(root, collections, inventory) if p.runs_eos_designs]
