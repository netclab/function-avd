"""The Request that pushes eos.cfg, offline."""

from __future__ import annotations

import json

import pytest

from function import push

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


def test_config_commands_drop_comments_blanks_and_end():
    assert push.config_commands(EOS_CLI) == [
        "hostname dc1-spine1",
        "router bgp 65100",
        "   router-id 192.168.255.1",
        "   neighbor SPINE peer group",
    ]


def test_a_banner_is_one_command_and_keeps_its_bang_lines():
    cli = "banner login\n!!!!\n!*** no entry ***!\nEOF\n\n!\nhostname x\n"

    assert push.config_commands(cli) == [
        {"cmd": "banner login", "input": "!!!!\n!*** no entry ***!"},
        "hostname x",
    ]


def test_an_indented_comment_is_one_command_without_its_indent():
    # As eos_cli_config_gen writes raw eos_cli under an SVI, in AVD's twodc scenario.
    cli = (
        "interface Vlan112\n"
        "   comment\n"
        "   Comment created from raw_eos_cli\n"
        "   EOF\n"
        "\n"
        "   ip address virtual 10.1.12.1/24\n"
    )

    assert push.config_commands(cli) == [
        "interface Vlan112",
        {"cmd": "   comment", "input": "Comment created from raw_eos_cli"},
        "   ip address virtual 10.1.12.1/24",
    ]


def test_a_block_with_no_end_is_refused():
    with pytest.raises(ValueError, match="EOF"):
        push.config_commands("banner motd\nhello\n")


def test_the_push_is_one_session_ending_in_the_digest():
    body = json.loads(push.push_body(push.config_commands(EOS_CLI), HASH))
    cmds = body["params"]["cmds"]

    assert cmds[:3] == ["enable", "configure session", "rollback clean-config"]
    assert cmds[-3:] == [push.marker_line(HASH), "commit", "show running-config digest"]
    assert body["id"] == f"push-{REV}"


def test_observe_reads_marker_and_digest_as_text():
    body = json.loads(push.observe_body(HASH))

    assert body["params"]["format"] == "text"
    assert body["params"]["cmds"] == [
        "enable",
        "show running-config | include alias avd_cfg_",
        "show running-config digest",
    ]


def test_before_a_digest_the_check_trusts_the_marker():
    logic = push.expected_check_logic(HASH, None)

    assert f"avd_cfg_{REV} " in logic
    assert "result[2]" not in logic


def test_with_a_digest_the_check_wants_both():
    logic = push.expected_check_logic(HASH, "d1gest")

    assert f"avd_cfg_{REV} " in logic
    assert '"d1gest"' in logic


def observed(body: dict, sent: str = "") -> dict:
    return {
        "status": {
            "response": {"body": json.dumps(body)},
            "requestDetails": {"body": sent},
        }
    }


def pushed(rev: str = REV) -> str:
    return f'{{"id":"push-{rev}","jsonrpc":"2.0"}}'


def test_a_push_response_gives_the_digest():
    request = observed({"result": [{}, {}, {}, {}, {"digest": "abc123"}]}, pushed())

    assert push.digest_from_observed(request, HASH) == ("abc123", "push")


def test_a_push_response_of_another_revision_gives_nothing():
    request = observed({"result": [{}, {"digest": "stale"}]}, pushed("0000000000000000"))

    assert push.digest_from_observed(request, HASH) is None


def test_an_observe_response_gives_the_digest_under_this_marker():
    result = [{"output": ""}, {"output": f"alias avd_cfg_{REV} show clock\n"}, {"output": "abc\n"}]

    assert push.digest_from_observed(observed({"result": result}), HASH) == ("abc", "observe")


def test_an_observe_response_under_another_marker_gives_nothing():
    other = "alias avd_cfg_0000000000000000 show clock\n"
    result = [{"output": ""}, {"output": other}, {"output": "abc\n"}]

    assert push.digest_from_observed(observed({"result": result}), HASH) is None


def test_no_response_gives_nothing():
    assert push.digest_from_observed({}, HASH) is None
    assert push.digest_from_observed({"status": {"response": {"body": "nope"}}}, HASH) is None
    assert push.error_from_observed({}, HASH) is None


REFUSED = {
    "error": {
        "code": 1002,
        "message": "CLI command 4 of 9 'interface Ethernet99' failed: invalid command",
        "data": [{}, {}, {}, {"errors": ["Invalid input (at token 1: 'Ethernet99')"]}],
    }
}


def test_a_refused_push_gives_eapis_message_and_the_commands_errors():
    error = push.error_from_observed(observed(REFUSED, pushed()), HASH)

    assert error == (
        "CLI command 4 of 9 'interface Ethernet99' failed: invalid command: "
        "Invalid input (at token 1: 'Ethernet99')"
    )


def test_a_push_that_succeeded_gives_no_error():
    request = observed({"result": [{}, {"digest": "abc"}]}, pushed())

    assert push.error_from_observed(request, HASH) == ""


def test_an_observe_or_another_revisions_push_says_nothing_of_the_error():
    assert push.error_from_observed(observed(REFUSED), HASH) is None
    assert push.error_from_observed(observed(REFUSED, pushed("0000000000000000")), HASH) is None


def request(**given) -> dict:
    args = {
        "name": "single-dc-l3ls-dc1-spine1",
        "namespace": "l3ls",
        "url": "https://dc1-spine1.l3ls.svc:443/command-api",
        "secret_name": "single-dc-l3ls-eapi",
        "secret_key": "default",
        "insecure_skip_tls_verify": True,
        "commands": push.config_commands(EOS_CLI),
        "config_hash": HASH,
        "deployed_digest": None,
    }
    return push.request_object(**{**args, **given})


def test_the_request_pushes_with_the_secrets_key_and_never_deletes():
    spec = request()["spec"]
    for_provider = spec["forProvider"]
    by_action = {m["action"]: m for m in for_provider["mappings"]}

    assert spec["managementPolicies"] == ["Observe", "Create", "Update"]
    assert "providerConfigRef" not in spec
    assert set(by_action) == {"CREATE", "UPDATE", "OBSERVE"}
    assert by_action["CREATE"]["body"] == by_action["UPDATE"]["body"]
    assert for_provider["headers"]["Authorization"] == [
        "Basic {{ single-dc-l3ls-eapi:l3ls:default }}"
    ]
    for mapping in for_provider["mappings"]:
        json.loads(mapping["body"])  # a JSON literal is a jq program too
