# Talos Patching

This directory contains Kustomization patches that are added to the talhelper configuration file.

<https://www.talos.dev/v1.7/talos-guides/configuration/patching/>

## Patch Directories

Under this `patches` directory, there are several sub-directories that can contain patches that are added to the talhelper configuration file.
Each directory is optional and therefore might not created by default.

- `global/`: patches that are applied to both the controller and worker configurations
- `controller/`: patches that are applied to the controller configurations
- `worker/`: patches that are applied to the worker configurations
- `${node-hostname}/`: patches that are applied to the node with the specified name

### Coral Edge TPU (PCIe)

Stock Talos does **not** ship the kernel drivers needed for the Coral Edge TPU. The devices use the **gasket** (framework) and **apex** (device) modules.

1. **Use an image that includes the modules** — either:
   - a custom installer built with [talos-pkgs-gasket](https://github.com/ammmze/talos-pkgs-gasket), or
   - a [Talos system extension](https://www.talos.dev/v1.11/advanced/kernel-module/) that adds the gasket/apex modules (built and signed with the same kernel).

2. **Load the modules** — enable the controller patch so the node with the Coral loads them:
   - In `talconfig.yaml`, under `controlPlane.patches`, add:
     `- "@./patches/controller/machine-kernel-modules-coral.yaml"`
   - That patch is in `controller/machine-kernel-modules-coral.yaml` and lists `gasket` and `apex` (plus the existing global modules).

Do **not** enable the Coral patch when using the stock Talos installer, or modprobe will fail for missing modules (may break or warn at boot).
