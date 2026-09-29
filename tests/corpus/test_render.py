"""What the Fabric renders of AVD's own repositories, against what AVD commits."""

from __future__ import annotations

from function import fabric


def test_the_render_succeeds(rendered: fabric.Render):
    assert rendered.problem is None


def test_every_host_rendered_has_a_golden_and_equals_it(rendered: fabric.Render, golden):
    assert rendered.structured
    for host, structured in rendered.structured.items():
        assert host in golden, host
        assert structured == golden[host], host


def test_every_host_rendered_has_what_its_push_needs(rendered: fabric.Render):
    assert set(rendered.push) == set(rendered.structured)
    for host, push in rendered.push.items():
        assert push["host"], host


def test_the_fabric_composes_one_device_per_host(emitted, rendered: fabric.Render):
    fab, required = emitted

    composed = fabric.compose(fab, required, {}, lambda *_: rendered)

    devices = [d.resource for d in composed.resources.values() if d.resource["kind"] == "Device"]
    assert composed.status["error"] == ""
    assert len(devices) == len(rendered.structured)
