# Authentik configuration as blueprints, not Terraform

Authentik applications, providers, access groups and their bindings are kept in this repo as Authentik blueprints, shipped to Authentik through a Flux-managed ConfigMap. We chose blueprints over the Authentik Terraform/OpenTofu provider because the cluster is already GitOps through Flux, blueprints are applied by Authentik itself with no state file or extra runner, and one blueprint (social login) already works this way. Users and their group memberships stay in the Authentik UI, so no personal details land in this public repo.

## Consequences

Blueprints create and update but do not remove objects that drop out of a file; removal needs an explicit `state: absent` entry.
