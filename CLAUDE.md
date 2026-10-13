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
- **Platform namespaces have Cilium policies too** (CIS AKS 4.4.2): `external-dns` (API server, Entra ID,
  ARM), `external-secrets` (API server, Entra ID, Key Vault; webhook 10250 in from the cluster) and
  `platform-gateway` (443 in, notification-controller 9292 and istiod out), next to the zones'.
  Every namespace the fleet creates, and flux-system, sets its `default` ServiceAccount's
  `automountServiceAccountToken: false` (CIS 4.1.5); `default`, `kube-public` and `kube-node-lease` are labelled
  PSS restricted with a deny-all NetworkPolicy (`infrastructure/configs/kubernetes-namespaces.yaml`, prune disabled).
- **GPU NodePools** (`infrastructure/node-pools`, `<zone>gpu`): NVIDIA H100 v5 on-demand, taint
  `platform/accelerator=nvidia-gpu`, AKS managed GPU stack (AKSNodeClass tag `EnableManagedGPUExperience`).
  Apps label pods `platform/accelerator: nvidia-gpu` and request `nvidia.com/gpu`; the policy mutation adds
  the toleration. **No GPU nodes in the dev lab**: `gpu_cpu_limit: "0"` in the cells' cluster-settings.yaml
  (default 0 if unset) - the user does not want them run here.
- **Observability** (`infrastructure/observability`): Grafana k8s-monitoring 4.5.2 (Alloy, kube-state-metrics,
  OpenCost) in `infra-observability`; stack endpoints + token from the vault secret `flux-system--grafana-cloud`
  (JSON, see its key-vault.yaml) via `valuesFrom`. `infra-observability` is a platform namespace in the
  landing zone's exclusions.
- **Every namespace carries `platform/team`, `platform/cost-center` and annotation `platform/contact`**
  (Azure Policy `namespace-ownership`); the platform's own are `platform`.
- `clusters/dev-stateless-az2` stays even while that cluster is destroyed.
