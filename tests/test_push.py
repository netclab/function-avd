"""The eAPI push Request builders (push.py), offline.

These pin the contract the live provider-http loop depends on: what a config
session may contain, which observed responses are allowed to define the golden
digest, and the shape of the composed Request. The semantics were probed
against a live cEOS lab (session + alias marker + `show running-config digest`
over JSON-RPC) before being encoded here.
"""

from __future__ import annotations

import json

import pytest

from avd_live_model import push

HASH = "sha256:196c053fa8537e1c"
REV = "196c053fa8537e1c"

EOS_CLI = """!RANCID-CONTENT-TYPE: arista
!
hostname dc1-spine1
!
router bgp 65100
   router-id 192.168.255.1
   neighbor SPINE peer group
!
end
"""


def test_config_commands_strip_comments_blanks_and_end() -> None:
    cmds = push.config_commands(EOS_CLI)
    assert cmds == [
        "hostname dc1-spine1",
        "router bgp 65100",
        "   router-id 192.168.255.1",
        "   neighbor SPINE peer group",
    ]


def test_config_commands_reject_multiline() -> None:
    # `banner motd` swallows following lines until EOF; flattened into runCmds
    # it would corrupt the whole session. Refuse rather than push garbage.
    with pytest.raises(ValueError, match="banner"):
        push.config_commands("banner motd\nhello\nEOF\n")


def test_push_body_is_one_atomic_session_ending_in_digest() -> None:
    body = json.loads(push.push_body(EOS_CLI, HASH))
    cmds = body["params"]["cmds"]
    assert cmds[0] == "enable"
    # Unnamed on purpose: EOS keeps one completed session in history, so a
    # fixed name breaks the second push of the same revision (drift reclaim).
    assert cmds[1] == "configure session"
    assert cmds[2] == "rollback clean-config"
    # The revision rides in the JSON-RPC id -- it is how a push response in
    # requestDetails is recognized as belonging to this configHash.
    assert body["id"] == f"push-{REV}"
    assert cmds[-3] == push.marker_line(HASH)
    assert cmds[-2] == "commit"
    # The push response must carry the digest of the config just committed, so
    # the function can record it without a second round-trip.
    assert cmds[-1] == "show running-config digest"


def test_observe_body_reads_marker_and_digest_as_text() -> None:
    body = json.loads(push.observe_body(HASH))
    assert body["params"]["format"] == "text"  # `| include` has no JSON form
    assert body["params"]["cmds"] == [
        "enable",
        "show running-config | include alias avd_cfg_",
        "show running-config digest",
    ]


def test_check_logic_before_first_digest_trusts_the_marker() -> None:
    logic = push.expected_check_logic(HASH, None)
    assert f"avd_cfg_{REV} " in logic
    assert "result[2]" not in logic  # no digest to compare yet


def test_check_logic_with_digest_requires_both() -> None:
    logic = push.expected_check_logic(HASH, "d1gest")
    assert f"avd_cfg_{REV} " in logic
    assert '"d1gest"' in logic
    assert 'rtrimstr("\\n")' in logic  # text-format outputs keep their newline


def _observed(response_result, request_body=""):
    return {
        "status": {
            "response": {"body": json.dumps({"result": response_result})},
            "requestDetails": {"body": request_body},
        }
    }


def test_digest_from_push_response_is_authoritative() -> None:
    observed = _observed(
        [{}, {}, {}, {}, {"digest": "abc123"}],
        request_body=f'{{"id":"push-{REV}","jsonrpc":"2.0"}}',
    )
    assert push.deployed_digest_from_observed(observed, HASH) == ("abc123", "push")


def test_push_response_for_another_revision_is_ignored() -> None:
    # A lagging status still shows the previous revision's push; recording its
    # digest against the new configHash would stop the new config from landing.
    observed = _observed(
        [{}, {"digest": "stale"}],
        request_body='{"id":"push-0000000000000000","jsonrpc":"2.0"}',
    )
    assert push.deployed_digest_from_observed(observed, HASH) is None


def test_digest_from_observe_is_vouched_by_the_marker() -> None:
    observed = _observed(
        [
            {"output": ""},
            {"output": f"alias avd_cfg_{REV} show clock\n"},
            {"output": "abc123\n"},
        ]
    )
    assert push.deployed_digest_from_observed(observed, HASH) == ("abc123", "observe")


def test_observe_with_foreign_marker_yields_nothing() -> None:
    observed = _observed(
        [
            {"output": ""},
            {"output": "alias avd_cfg_0000000000000000 show clock\n"},
            {"output": "abc123\n"},
        ]
    )
    assert push.deployed_digest_from_observed(observed, HASH) is None


def test_empty_or_malformed_status_yields_nothing() -> None:
    assert push.deployed_digest_from_observed({}, HASH) is None
    assert push.deployed_digest_from_observed({"status": {"response": {"body": "nope"}}}, HASH) is None


def test_request_object_shape() -> None:
    req = push.request_object(
        name="lab-dc1-spine1-push",
        namespace="default",
        labels={"avd.netclab.dev/device": "dc1-spine1"},
        url="https://dc1-spine1.default.svc/command-api",
        credentials_secret="eapi-creds",
        provider_config="eapi",
        insecure_skip_tls_verify=True,
        eos_cli=EOS_CLI,
        config_hash=HASH,
        deployed_digest=None,
    )
    assert req["apiVersion"] == "http.m.crossplane.io/v1alpha2"
    assert req["kind"] == "Request"
    # Deleting a Device stops managing the box, it does not wipe it: no REMOVE
    # mapping, and no Delete management policy so the box is orphaned.
    assert req["spec"]["managementPolicies"] == ["Observe", "Create", "Update"]
    fp = req["spec"]["forProvider"]
    assert {m["action"] for m in fp["mappings"]} == {"CREATE", "UPDATE", "OBSERVE"}
    # CREATE and UPDATE are the same full replace -- a device is never half-made.
    by_action = {m["action"]: m for m in fp["mappings"]}
    assert by_action["CREATE"]["body"] == by_action["UPDATE"]["body"]
    assert all(m["method"] == "POST" for m in fp["mappings"])  # eAPI is POST-only
    assert fp["headers"]["Authorization"] == ["Basic {{ eapi-creds:default:basic }}"]
    assert fp["expectedResponseCheck"]["type"] == "CUSTOM"
    # Every mapping body must be valid JSON: a JSON literal is also a valid jq
    # program, which is what provider-http evaluates bodies as.
    for m in fp["mappings"]:
        json.loads(m["body"])
