# OneK8s fleet - notes for Claude sessions

The shared working notes for all OneK8s repos (deployment workflows, current state of dev, decisions
and gotchas) are in `onek8s/aks-terraform/CLAUDE.md`. Read that first.

This repo is what Flux deploys into every cluster (`clusters/<cluster>/`, pulling `infrastructure/`
and `tenants/`). Specific to it:

- **No GitHub CI runs here.** Validate locally: `kustomize build` of every changed directory, and
  `python3 scripts/check-egress.py` for egress allow-lists. Flux applies `main` within minutes of a
  merge (webhook receivers); check the result with aks-terraform's Cluster status workflow.
- **Internal and external never share a cluster** (design D52): `clusters/dev-ext-*` are external - zones
  ext-fi/ext-se only, `node-pools` on `infrastructure/node-pools/external`, no `external-dns/internal`.
  `clusters/dev-int-stateless-az2` is internal and **the lab's running cell since 2026-10-15** (germanywestcentral,
  behind labfw in the hub; its twin `dev-int-stateless-az1` is disabled - zone 1 is not available to the subscription): zone int-fi only (Sweden disabled), `node-pools-int-fi` + `zone-int-fi` in zones.yaml,
  `external-dns-internal`, `flux-system-key-vault` (no `platform-gateway`, no `flux-webhook`: a public LB cannot
  answer behind the firewall), tenants `tenants/int-fi-example`.
- **`clusters/dev-int-stateless-az2` is overlay + the hub firewall only, without ACNS** (design D61, 2026-10-16): Cilium has no
  DNS proxy there, so its Kustomizations patch every Cilium policy to layer 4 (`spec.patches`: the DNS rules removed, the
  `toFQDNs` egress replaced by HTTPS to `world`) - the hub firewall decides the names; `firewall-egress-int-fi` lets int-fi's
  apps out. The firewall sees the zone's node addresses (overlay), so its rules are per zone. Validate such patches by
  building a temporary kustomization with the Kustomization's path as resource and its patches. A new Cilium policy with
  DNS or FQDN rules needs a patch here too.
- **`clusters/dev-int-stateless-az3` is the firewall-egress cell** (design D59, 2026-10-15): no overlay (Azure CNI node
  subnet; pods in their zone's subnet of 100.64.0.0/16), Sweden's int-se only, tenants `tenants/int-se-example`. Its zones
  also get `firewall-egress-<zone>` (`infrastructure/zones/firewall-egress`): a platform CCNP letting the zone's
  application namespaces out to `world` and resolve any name - labfw decides, by zone and per application
  (azure-landing-zone `firewall-applications/`, proposed by aks-ai-agents' egress agent). Cilium keeps the in-cluster rules.
- **Countries can be disabled**: NodePools are one directory per zone (`node-pools/<internal|external>/<zone>`); a
  cell running only some countries lists `node-pools-<zone>` + `zone-<zone>` per enabled country in its zones.yaml.
- **Values come from aks-terraform** through `clusters/<cluster>/cluster-settings.yaml` (`${...}`
  substitutions such as `nap_subnet_id_<zone>`, `key_vault_url_<ns>`, `nap_availability_zone`).
- **Isolation zones** (`infrastructure/zones`, applied once per zone via `zones.yaml`): Gateway
  (class `${gateway_class_name}` from aks-terraform's `gateway_implementation`: `traefik-<zone>`, Traefik
  deployed by aks-terraform's own `gateways/traefik` - not from here - or `approuting-istio`, AKS App Routing,
  options in the ConfigMap `gateway` - only through the kustomize component `gateway-implementations/approuting-istio`,
  since Traefik refuses a `parametersRef`; zones.yaml / infrastructure.yaml pick `./gateway-implementations/${gateway_implementation}`; istiod's 1 CPU / 4 GiB, Azure/AKS#5939, is why dev runs Traefik), ESO
  (`controllerClass: <zone>`), certificate, Cilium baseline. Their gateway pods and
  ESO run on the **system pool** (`kubernetes.azure.com/mode: system`, toleration
  `CriticalAddonsOnly`) so a zone without applications has no nodes.
- **NodePools** (`infrastructure/node-pools/{internal,external}`): per zone an on-demand and a spot NodePool (spot
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
  `platform-gateway` (443 in, notification-controller 9292 and istiod out; Traefik's API server access is
  aks-terraform's policy `gateway-traefik`), next to the zones'.
  Every namespace the fleet creates, and flux-system, sets its `default` ServiceAccount's
  `automountServiceAccountToken: false` (CIS 4.1.5); `default`, `kube-public` and `kube-node-lease` are labelled
  PSS restricted with a deny-all NetworkPolicy (`infrastructure/configs/kubernetes-namespaces.yaml`, prune disabled).
- **GPU NodePools** (`infrastructure/node-pools`, `<zone>gpu`): NVIDIA H100 v5 on-demand, taint
  `platform/accelerator=nvidia-gpu`, AKS managed GPU stack (AKSNodeClass tag `EnableManagedGPUExperience`).
  Apps label pods `platform/accelerator: nvidia-gpu` and request `nvidia.com/gpu`; the policy mutation adds
  the toleration. **No GPU nodes in the dev lab**: `gpu_cpu_limit: "0"` in the cells' cluster-settings.yaml
  (default 0 if unset) - the user does not want them run here.
- **Observability** (`infrastructure/observability`): Grafana k8s-monitoring 4.5.2 as Grafana Cloud's Fleet
  Management sets it up (user's choice, D48): `alloy-daemon` DaemonSet (privileged, hostNetwork/PID, `large`
  preset with requests lowered to 100m/256Mi - the user's choice over a 3rd system node - and
  `system-node-critical` so a full system node makes room for it) with **remote config** - pipelines are managed in Grafana Cloud, not here -
  plus node-exporter, kube-state-metrics, Beyla k8s cache; OpenCost off until the vault secret has
  a metrics:read token (prometheus-*). Secret `flux-system--grafana-cloud` (JSON, see key-vault.yaml)
  via `valuesFrom` - as one `values.yaml` that ESO templates with every value quoted: **Flux's
  `valuesFrom.targetPath` parses like `helm --set`, so a numeric user ID became `1.872197e+06`** and Fleet
  Management refused it. ESO's template data is `map[string]string`: sprig `hasKey` fails, use `index`. `infra-observability` is a platform namespace in the landing zone's exclusions. The zone
  NSGs do not open Alloy's clustering port 12345 between subnets. On AKS the chart refuses to render unless every Alloy
  collector (and kube-state-metrics) has the pod annotation `kubernetes.azure.com/set-kube-service-host-fqdn: "true"`;
  it detects AKS from live node labels (`lookup`), so an offline `helm template` does not catch it.
- **Every namespace carries `platform/team`, `platform/cost-center` and annotation `platform/contact`**
  (Azure Policy `namespace-ownership`); the platform's own are `platform`.
- **Namespace access** (`infrastructure/namespace-access/{view,edit,admin}`, one `base`): a RoleBinding template (`platform-${role}`,
  ClusterRole `${role}` = view / edit / admin, subject the Entra ID group `${group_object_id}`). No cluster
  pulls it in: aks-terraform gives each namespace with groups a Flux configuration `<ns>-access`, one Kustomization per
  `managed_namespaces.<ns>.groups.<role>` on that role's directory (Azure refuses two Kustomizations of one configuration on one path).
- `clusters/dev-ext-stateless-az1`, `-az2` and `dev-ext-stateful` stay (external cells, none running).
- **The dev cells run one 8 GiB system node** (2026-10-14): Traefik instead of App Routing's istiod and no VPA add-on
  (aks-terraform tfvars). Everything on the system pool shares ~5.8 GiB allocatable with ~2 GiB to spare: keep new
  platform requests small.
