# function-avd

function-avd runs Arista AVD in Kubernetes. It builds each device's configuration from
an AVD repository, pushes it to the device over eAPI, and pushes it again if someone
changes the device by hand.

Two Crossplane packages:

- `configuration-avd`: the API, with the kinds `Fabric`, `FabricInput` and `Device`.
- `function-avd`: the function that runs AVD. `configuration-avd` installs it.

[netadopt](https://github.com/netclab/netadopt) writes the resources from any AVD
repository. [example/single-dc-l3ls.yaml](example/single-dc-l3ls.yaml) is what it writes
for AVD's `single-dc-l3ls` example.

## Installing

Needs Crossplane v2.4.0 or later. Apply, in this order:

1. [example/runtime.yaml](example/runtime.yaml): the function's CPU and memory.
2. The Configuration, with a version from
   [Releases](https://github.com/netclab/function-avd/releases):

   ```yaml
   apiVersion: pkg.crossplane.io/v1
   kind: Configuration
   metadata:
     name: configuration-avd
   spec:
     package: xpkg.upbound.io/netclab/configuration-avd:<version>
   ```

3. [example/providerconfig.yaml](example/providerconfig.yaml), once the Configuration
   is healthy: it needs provider-http, which the Configuration installs.

If the cluster pulls packages through a mirror, change the prefix in `runtime.yaml` to
the mirror's address.

## Before applying a Fabric

**Applying a Fabric changes the devices.** Each device is configured at the address in
its Ansible variables (`ansible_host`, `ansible_user`, `ansible_password`, and the
`ansible_httpapi_*` settings). To use a lab instead, override them in
`spec.extraVars`, as
[`netadopt avd lab --extra-vars-out`](https://github.com/netclab/netadopt#building-a-lab-from-the-fabric)
writes them.

- A push replaces the whole running configuration.
- The configuration must keep eAPI on, or the device is unreachable after the first push.
- Deleting a Device or a Fabric stops the pushes and leaves the configuration on the
  device.
- The build runs Ansible on the repository's variables inside the cluster, so anything
  in them runs there too.

## Status

```yaml
# Fabric
status:
  missing: {inputs: [], configMaps: []}   # what it lists and cannot find; the build waits
  error: ""                               # why the last build failed; Devices keep their config
---
# Device
status:
  configHash: sha256:9f2c...              # the eos.cfg built from spec.structuredConfig
  invalid: []                             # problems in the structured config; if any, no push
  error: ""                               # eAPI's error from the last push
  deployed: {configHash: sha256:9f2c...}  # the config the device runs
```

## How long a change takes

Usually a second plus the build. Creating a fabric opens Crossplane's circuit breaker
for it (`Responsive=False`, `WatchCircuitOpen`); while it is open, a change takes up to
10 s plus the build. A larger `--circuit-breaker-burst` in Crossplane's Helm `args`
keeps it closed for larger fabrics.

A build of 26 devices takes 16 s on 10 cores and 54 s on 2. Crossplane stops a build
after 120 s.

## Limits

- Each release uses one AVD version: the `pyavd` version in `pyproject.toml`.
- Only `arista.avd` and the collections it needs are installed.
- `custom_templates` in `eos_cli_config_gen` are not supported yet.

## License

[Apache-2.0](LICENSE). Uses [Arista AVD](https://github.com/aristanetworks/avd), also
Apache-2.0.
