# OneK8s fleet - notes for Claude sessions

The shared working notes for all OneK8s repos (deployment workflows, current state of dev, decisions
and gotchas) are in `onek8s/aks-terraform/CLAUDE.md`. Read that first.

This repo is what Flux deploys into every cluster (`clusters/<cluster>/`, pulling `infrastructure/`
and `tenants/`). Specific to it:

- **Validate locally**: `kustomize build` of every changed directory (the CI workflow does the same) and
  `python3 scripts/check-egress.py <paths>` for application network policies (the landing zone's `app-egress` rule). Flux applies `main` within minutes of a merge
  (every lab cell polls); check the result with aks-terraform's Cluster status workflow.
- **Clusters**: `clusters/dev-int-stateless-az3` is the lab's one cell (design v2.0): internal, Sweden's
  int-se only, Azure CNI Pod Subnet behind the hub's labfw - no platform gateway, no Flux webhook;
  tenants `tenants/int-se-example`. `clusters/dev-int-stateful` is defined, never deployed. Internal and
  external never share a cluster (design D52).
- **Every cell is a pod-subnet cell**: the pods keep their addresses up to the hub firewall
  (`pod-addresses-to-firewall`: AKS's ip-masq-agent translates nothing), and each zone gets
  `firewall-egress-<zone>` (`infrastructure/zones/firewall-egress`): its application namespaces go out to
  `world` and resolve any name - the firewall decides per application (azure-landing-zone
  `firewall-applications/`, proposed by aks-ai-agents' egress agent). Cilium keeps the in-cluster rules.
- **Zone nodes are aks-terraform's classic pools** (`zone_node_pools`: `<zone>`, `<zone>spot`, `<zone>arm`,
  `<zone>armspot`); `infrastructure/autoscaler-priority` (the cluster autoscaler's priority expander) puts
  the spot pools first. No NAP / NodePools: NAP does not support a pod subnet.
- **Values come from aks-terraform** through `clusters/<cluster>/cluster-settings.yaml` (`${...}`
  substitutions such as `key_vault_url_<ns>`).
- **Isolation zones** (`infrastructure/zones`, applied once per zone via `zones.yaml`): Gateway (class
  `${gateway_class_name}` from aks-terraform's `gateway_implementation`: `traefik-<zone>`, deployed by
  aks-terraform's `gateways/traefik`, or `approuting-istio`, whose ConfigMap `gateway` comes only through the
  kustomize component `gateway-implementations/approuting-istio` - Traefik refuses a `parametersRef`), ESO
  (`controllerClass: <zone>`), certificate, Cilium baseline (default deny; DNS for `cluster.local` and
  `internal.cloudapp.net`, the latter so musl resolvers do not stop on the node's search suffix). Gateway
  pods and ESO run on the **system pool**, so a zone without applications has no nodes.
- `policy/egress-allow-lists.yaml` is also read by aks-ai-agents' traffic flow documentation (design 5.3).
- **Image Integrity** (`infrastructure/image-verification`): Ratify's KeyManagementProvider, Store and
  Verifier, all named `platform`; trusts only the platform certificate's subject.
- **Platform namespaces have Cilium policies too** (CIS AKS 4.4.2), every namespace the fleet creates sets
  its `default` ServiceAccount's `automountServiceAccountToken: false`, and `default`, `kube-public`,
  `kube-node-lease` are PSS restricted with a deny-all NetworkPolicy (`infrastructure/configs`).
- **Observability** (`infrastructure/observability`): Grafana k8s-monitoring with Fleet Management remote
  config (D48); secret `flux-system--grafana-cloud` as one ESO-templated `values.yaml` (Flux's
  `valuesFrom.targetPath` parses like `helm --set`: a numeric ID became `1.872197e+06`); ESO's template
  data is `map[string]string` (use `index`, not `hasKey`). Every Alloy collector and kube-state-metrics
  needs the AKS annotation `kubernetes.azure.com/set-kube-service-host-fqdn: "true"`.
- **Every namespace carries `platform/team`, `platform/cost-center` and annotation `platform/contact`**
  (Azure Policy `namespace-ownership`); the platform's own are `platform`.
- **Namespace access** (`infrastructure/namespace-access/{view,edit,admin}`): RoleBinding templates that
  aks-terraform's `<ns>-access` Flux configurations apply (one per namespace: Azure refuses two
  Kustomizations of one configuration on one path).
- **The lab cell runs one 8 GiB system node** (Traefik, no VPA add-on): keep new platform requests small.
