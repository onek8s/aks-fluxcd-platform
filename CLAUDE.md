# OneK8s fleet - notes for Claude sessions

The shared working notes for all OneK8s repos (deployment workflows, current state of dev, decisions
and gotchas) are in `onek8s/aks-terraform/CLAUDE.md`. Read that first.

This repo is what Flux deploys into every cluster (`clusters/<cluster>/`, pulling `infrastructure/`
and `tenants/`). Specific to it:

- **No GitHub CI runs here.** Validate locally: `kustomize build` of every changed directory, and
  `python3 scripts/check-egress.py` for egress allow-lists. Flux applies `main` within minutes of a
  merge (webhook receivers); check the result with aks-terraform's Cluster status workflow.
- **Values come from aks-terraform** through `clusters/<cluster>/cluster-settings.yaml` (`${...}`
  substitutions such as `nap_subnet_id_<zone>`, `key_vault_url_<ns>`, `nap_availability_zone`).
- **Isolation zones** (`infrastructure/zones`, applied once per zone via `zones.yaml`): Gateway
  (class `approuting-istio`, AKS App Routing; options in the ConfigMap `gateway`), ESO
  (`controllerClass: <zone>`), certificate, Cilium baseline. Their gateway pods and
  ESO run on the **system pool** (`kubernetes.azure.com/mode: system`, toleration
  `CriticalAddonsOnly`) so a zone without applications has no nodes.
- **NodePools** (`infrastructure/node-pools`): per zone an on-demand and a spot NodePool (spot
  weight 50 + taint `platform/capacity=spot`), amd64 and arm64, scale to zero. Applications choose
  spot by tolerating the taint; NAP falls back to on-demand.
- **Cilium zone baseline** (`infrastructure/zones/cilium.yaml`): default deny; DNS only for
  `cluster.local` and `internal.cloudapp.net` names (the latter so musl resolvers do not stop on the
  node's search suffix); applications add their own FQDN egress policy.
- **Image Integrity** (`infrastructure/image-verification`): Ratify's KeyManagementProvider, Store and
  Verifier, all named `platform`; trusts only the platform certificate's subject. Ratify's CRDs come
  with the AKS add-on (aks-terraform `image_integrity_enabled`).
- `clusters/dev-stateless-az2` stays even while that cluster is destroyed.
