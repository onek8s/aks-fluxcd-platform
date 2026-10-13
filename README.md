# aks-fluxcd-platform

What Flux deploys into every cluster of [OneK8s](https://onek8s.github.io/) - one Kubernetes platform
for every cloud - built today as AKS clusters by
[aks-terraform](https://github.com/onek8s/aks-terraform). Everything here is plain Kubernetes, Flux,
Helm, Cilium and the Gateway API; only the few Azure facts in each cluster's `cluster-settings.yaml`
(vaults, subnets, identities) tie it to a cloud. Terraform installs the AKS Flux extension and a Flux
configuration named `platform` that points at `clusters/<cluster>`; everything else lives here.

The stateless clusters are the design's **cells**, and every cell runs every **isolation zone** -
`int-fi`, `int-se` (internal workloads of Finland and Sweden), `ext-fi`, `ext-se` (external ones) -
as a bulkhead of its own: the zone's own nodes in the zone's own subnet for its applications, its own gateway, its own
External Secrets Operator, its own Key Vault, and Cilium policies that keep its workloads apart
from every other zone's (design sections 4 to 7, 13 and 16).

## Layout

```
clusters/
  dev-stateless-az1/      # aks-dev-stateless-az1, a cell in availability zone 1
    cluster-settings.yaml # this cluster's values, for the Kustomizations that need them
    infrastructure.yaml   # Flux Kustomizations `infra-controllers`, `infra-configs`,
                          # `platform-gateway`, `external-dns`, `flux-webhook`, `node-pools`
                          # and `image-verification`
    zones.yaml            # Flux Kustomizations `zone-<zone>` -> infrastructure/zones, one per zone
    tenants.yaml          # Flux Kustomization `tenants` -> tenants
  dev-stateless-az2/      # aks-dev-stateless-az2, the same in availability zone 2
  dev-stateful/           # aks-dev-stateful: defined, not deployed - controllers and storage only
infrastructure/
  controllers/            # External Secrets Operator (CRDs, webhook, the platform's controller)
                          # and the Helm repositories
  configs/                # lets the namespace `admin`/`edit` roles manage HTTPRoutes
  zones/                  # one isolation zone: its gateway (Gateway, its options, certificate), its
                          # External Secrets Operator and its Cilium policies
  platform-gateway/       # the platform's own gateway: Flux's webhook receivers
  external-dns/           # external-dns, publishing the gateways' hostnames in Azure DNS (external)
                          # and Azure Private DNS (internal)
  flux-webhook/           # Flux's GitHub webhook receiver, on the platform gateway
  node-pools/             # the isolation zones' node auto provisioning NodePools and AKSNodeClasses
  image-verification/     # Ratify's configuration for AKS Image Integrity: the platform's signing
                          # certificate, the registry's signatures, the Notation trust policy
  storage/                # the `portable-disk` StorageClass, whose disks outlive the cluster
tenants/
  ext-fi-example/         # what the `example` team's application in the ext-fi zone needs in its
                          # namespace; its repository, onek8s/aks-fluxcd-example, is synced by
                          # aks-terraform's Flux configuration `ext-fi-example`
```

- `clusters/<cluster>/` is the path a cluster's `flux_git_repository.path` names. It holds Flux
  `Kustomization` objects only, each pulling in a shared directory, so adding a cluster is a new
  directory with the same files - and its own `cluster-settings.yaml`.
- `infrastructure/controllers/` comes first: its Kustomization waits for the controllers, so what
  needs their CRDs follows it with `dependsOn: [{ name: infra-controllers }]` - `infra-configs`, the
  zones (which also wait for `node-pools`), `platform-gateway`, `flux-webhook` (also after
  `platform-gateway`). `external-dns`, `node-pools` and `image-verification` follow nothing. Nothing waits for a
  zone, so one that does not come up holds back no other.
- Adding an isolation zone - a new country is two - is a `zone-<zone>` Kustomization in `zones.yaml`,
  a NodePool file in `infrastructure/node-pools`, and the zone in aks-terraform (`shared/`:
  `isolation_zones`, `zone_key_vault_names`; each cell: `isolation_zones`).

The Kustomizations use `sourceRef: { kind: GitRepository, name: platform }` - the GitRepository the
AKS Flux extension creates for the Flux configuration Terraform names `platform`.

## Controllers

| Controller | Namespace | Where | Chart |
| --- | --- | --- | --- |
| External Secrets Operator, the platform's: CRDs, webhook, controller `platform` | `external-secrets` | system pool | `external-secrets` 2.12.0 from https://charts.external-secrets.io |
| External Secrets Operator, a zone's: controller `<zone>` | `<zone>-secrets` | system pool | the same |
| A zone's gateway: Envoy, `<zone>-approuting-istio` | `<zone>-gateway` | system pool | none - AKS creates it for the Gateway (App Routing, below) |
| The platform's gateway: Envoy, `platform-approuting-istio` | `platform-gateway` | system pool | the same |
| external-dns, external and internal | `external-dns` | system pool | `external-dns` 1.23.x from https://kubernetes-sigs.github.io/external-dns/ |

The `HelmRelease` and `HelmRepository` objects live in `flux-system` and install into the namespace
in the table through `targetNamespace` and `storageNamespace` (the Helm release Secrets are kept
there too). The AKS Flux extension runs Flux multi-tenant: a HelmRelease or Kustomization without a
`serviceAccountName` is applied as the `flux-applier` service account of its own namespace, and only
`flux-system`'s exists, with cluster-wide rights. The extension also refuses references across
namespaces, so the HelmRepository sits next to the HelmRelease.

The system pool is tainted `CriticalAddonsOnly` (aks-terraform), so only the platform's own
controllers run there - they tolerate it and select `kubernetes.azure.com/mode: system`. That includes
every zone's gateway and External Secrets Operator, so a zone's own nodes are for its applications
only: node auto provisioning creates them for an application pod that selects `platform/zone: <zone>`
and tolerates the zone's taint, and removes them again when the zone has none - a zone without
applications costs no nodes.

## Isolation zones

`infrastructure/zones` is one zone, applied once per zone with the zone's values (`zones.yaml`):

| In the zone | What | Design |
| --- | --- | --- |
| `<zone>-gateway` | The Gateway `<zone>`, class `approuting-istio` - App Routing's Envoy Deployment and load balancer for it, shaped by the ConfigMap `gateway` (system pool, small requests, load balancer annotations, `externalTrafficPolicy: Local`): one HTTPS listener for `*.<zone>.dev.onek8s.lol` with the zone's wildcard certificate, open to routes from the zone's application namespaces only. Its load balancer is an internal one in `snet-<zone>-ilb` for an internal zone, a public one for an external zone | section 13 |
| `<zone>-secrets` | External Secrets Operator for the zone, `controllerClass: <zone>`: it syncs the stores that name `controller: <zone>` - the zone's - and no others. No CRDs, no webhook | section 6 |
| Cilium | The application baseline of the zone, the gateway's and the External Secrets Operator's policies - see [Network policies](#network-policies) | sections 5, 7, 16 |

A namespace belongs to a zone by its label `platform/zone: <zone>`; the zone's two platform
namespaces also carry `platform/role: platform`, which keeps them out of the application baseline and
off the Gateway's allowed routes. An application namespace is `<zone>-<app>` - an AKS managed
namespace of aks-terraform, labelled there.

## Network policies

Cilium enforces them - with ACNS's FQDN policies and eBPF host routing (aks-terraform:
`advanced_networking`) - as the third layer of design section 5, beside the cells' NSGs:

- **The application baseline**, a `CiliumClusterwideNetworkPolicy` per zone,
  `<zone>-application-baseline`, which no team can change. It selects every pod of the zone's
  application namespaces, so Cilium's default deny holds for everything it does not allow:
  - in: from the zone's own gateway pods, and from the node (probes) - not from another zone, another
    namespace or outside the cluster. The namespace's own pods reach each other through the managed
    namespace's default policy (`AllowSameNamespace`, aks-terraform).
  - out: DNS to CoreDNS for the cluster's own names (`*.cluster.local`, at every depth) - nothing
    else. No internet, no Key Vault, no other zone, no Azure instance metadata service
    (`169.254.169.254`, which has no name to allow).
- **An application's egress** is its own `CiliumNetworkPolicy` named `egress`, in the team's
  repository: every destination outside the cluster by FQDN with its ports, and the same names in its
  DNS rule (design section 16). A name it does not list it can neither resolve nor reach. CIDRs,
  entities and wildcards that open a whole cloud service are not allowed - the CI check below says
  so before a pull request is merged; Azure Policy is to enforce it at admission (design section 14).
- **The zone's gateway**: in on 443 from the load balancer (its clients keep their addresses, so
  they are `world` to Cilium) and from the node; out to the zone's application namespaces, istiod
  in `aks-istio-system` (15012, its configuration and certificates) and cluster DNS.
- **The zone's External Secrets Operator**: out to the API server, Entra ID
  (`login.microsoftonline.com`, the workload identity token exchange) and Key Vault
  (`*.vault.azure.net`) - the only namespaces of a zone that may name a vault.

`policy/egress-allow-lists.yaml` lists, per zone, the FQDNs an application of the zone may name - the
stand-in for the zone's Azure Firewall allow-list, which the lab's network mode has none of.
`scripts/check-egress.py` checks an application's `egress` policies against it and against the
rules above; the team's repository runs it in CI.

## Key Vault

aks-terraform gives every namespace that needs secrets its own share of a Key Vault: **a namespace
reads the secrets named `<namespace>--<name>`, and no others**. The vault enforces that itself, with
an Azure ABAC condition on the role assignment of each namespace's identity, so nothing in the
cluster has to be trusted to keep the namespaces apart. There are two kinds of vault (design
sections 6 and 13):

- **the platform vault** (`key_vault_url`): the gateways' certificates and the Flux webhook token;
- **a vault per zone** (`key_vault_url_<namespace>` of an application namespace): the secrets of
  the zone's applications.

In each namespace that uses a vault there is a `key-vault` service account, federated with that
namespace's identity (`key_vault_client_id_<namespace>`), and a `key-vault` SecretStore naming it and
the External Secrets Operator that serves it (`controller: platform` or `<zone>`). The operator
requests a token for the service account on every read, so an `ExternalSecret` in a namespace reads
that namespace's secrets only - and only the zone's operator reads them. No application pod runs as
`key-vault`, and none can reach a vault (the baseline).

| Namespace | Key Vault secret | Synced into |
| --- | --- | --- |
| `<zone>-gateway` | `<zone>-gateway--wildcard` in the platform vault - the zone's wildcard certificate | `gateway-tls`, the zone listener's certificate |
| `platform-gateway` | `platform-gateway--wildcard` in the platform vault - `*.onek8s.lol` | `gateway-tls` |
| `flux-system` | `flux-system--github-webhook-token` in the platform vault - created by aks-terraform | `github-webhook-token`, the token Flux's GitHub Receiver checks deliveries against |
| `ext-fi-example` | `ext-fi-example--github-webhook-token` in the ext-fi vault - the same token | `github-webhook-token`, for the team's own Receiver |

The certificates are issued and renewed by aks-terraform's **Renew Certificate** workflow (Let's
Encrypt, DNS-01 in the public zone), PKCS#12, which the `ExternalSecret`s unpack into
`kubernetes.io/tls` Secrets; a renewal reaches the listener within the hour.

The names of every secret can be listed by every identity with a share - only values are kept apart
- so no secret goes in a name.

## Publishing applications

Applications are published through the [Kubernetes Gateway API][gatewayapi] on their zone's
Gateway. aks-terraform turns on the managed Gateway API CRDs (Gateway API v1.5 on Kubernetes 1.36)
and [App Routing's Gateway API implementation][approutinggw] (`app_routing_gateway_api_enabled`): a
sidecar-less Istio control plane, `istiod` in `aks-istio-system`, serving the GatewayClass
`approuting-istio` - no service mesh. For each Gateway of that class it creates an Envoy
Deployment, a `LoadBalancer` Service, an autoscaler (at least two replicas) and a disruption budget
in the Gateway's namespace, shaped by the ConfigMap the Gateway names in
`spec.infrastructure.parametersRef`, within the add-on's customization allow list. AKS upgrades it
in place with the cluster.

What it does not offer: rate limits, request size limits and `EnvoyFilter` - the Gateway API has no
standard fields for them - nor customizing the access log (Envoy's default, on).

| | Who | Where |
| --- | --- | --- |
| The Gateway API CRDs | AKS / Platform | aks-terraform |
| The GatewayClass `approuting-istio` and its control plane | AKS | aks-terraform |
| The zone's Gateway, its options (node pool, load balancer), its listener and certificate | Platform | `infrastructure/zones` |
| DNS: a record per route hostname | Platform | `infrastructure/external-dns` |
| `HTTPRoute`, `Service`, the app, its egress policy | Team | the team's own repository |

- **No plain HTTP anywhere**: one HTTPS listener on 443, no redirect listener, port 443 alone open in
  the NSGs.
- A team's route attaches to its zone's Gateway (`namespace: <zone>-gateway`, `name: <zone>`) and
  names its hostnames, below the zone's domain - which aks-terraform gives the team's Flux
  configuration as `${zone_domain}`. A route in another zone's namespace is not accepted.
- **DNS** (`infrastructure/external-dns`), two releases, each keeping a TXT record beside every
  record marking it as its own, so neither ever deletes a record it did not create:
  - `external-dns-ext`: the routes of the Gateways labelled `platform/exposure: ext` - the external
    zones' and the platform's - as A records to the gateway's public address, in the public zone.
  - `external-dns-int`: the routes of `platform/exposure: int` Gateways, as A records to the internal
    load balancer's address, in the internal zones' Azure Private DNS zones
    (`int-fi.dev.onek8s.lol`, ...; aks-terraform's `shared/`), which resolve inside the cells only.

  Both act as the `<cluster identity>-dns` identity aks-terraform creates, federated with
  `external-dns/external-dns`.

### One hostname in every cell

An external application has one name that every cell serves - `hello.ext-fi.dev.onek8s.lol` - behind
**Azure Traffic Manager** (design section 8): aks-terraform keeps it as a CNAME to a Traffic Manager
profile (`shared/`, `global_hostnames`) whose endpoints are the application's own name in each cell
(`hello-<cluster>.ext-fi.dev.onek8s.lol`, an A record of that cell's ext-fi gateway), weighted alike,
with an HTTPS probe of `/healthz` on the global name - so DNS hands out one cell or the other, and
leaves out one that stops answering. The route that names the global name carries
`external-dns.kubernetes.io/controller: traffic-manager`, which external-dns leaves alone. The page
says which cluster answered.

### Cluster settings

The values that differ by cluster are Flux variables, kept in `clusters/<cluster>/cluster-settings.yaml`
and handed to the Kustomizations through `postBuild.substituteFrom`:

| Variable | Set by | |
| --- | --- | --- |
| `key_vault_url`, `key_vault_url_<namespace>`, `key_vault_client_id_<namespace>` | aks-terraform | The platform vault, a namespace's vault, and the identity each namespace's `key-vault` service account reads its share as. The zones' gateways' client IDs go straight into `zones.yaml`. |
| `dns_zone_name`, `dns_zone_resource_group_name`, `dns_zone_subscription_id`, `dns_identity_client_id`, `internal_dns_resource_group_name` | aks-terraform | The public zone, the internal zones' resource group, and the identity that writes them. |
| `cluster_name`, `azure_tenant_id` | aks-terraform | external-dns's record owner, and the tenant the identities live in. |
| `nap_subnet_id_<zone>`, `nap_availability_zone` | aks-terraform | Where node auto provisioning puts each zone's nodes: the zone's own subnet, and the cell's availability zone (`swedencentral-1`). |
| `flux_webhook_hostname` | this repository | The platform gateway's hostname, this cluster's own, below `onek8s.lol`. |

The ones aks-terraform sets are Azure facts and are never written here: it gives them to the
cluster's Flux configuration as post-build variables of the `platform` Kustomization, which fills
them into `cluster-settings.yaml` as it applies it.

### Webhook

A push to this repository reaches the cluster at once rather than at the next poll: GitHub calls
Flux's webhook receiver, which reconciles the `platform` GitRepository (`infrastructure/flux-webhook`).
Its route sends `/hook/` of `flux_webhook_hostname`, on the platform Gateway, to
notification-controller's `webhook-receiver`; nothing else of the receiver is routed. The `github`
Receiver checks each delivery's signature against `flux-system--github-webhook-token`.

The webhooks are not set by hand: aks-terraform's **Flux webhooks** workflow runs after every Deploy
and points a push webhook per cluster at its `flux_webhook_hostname`, on two repositories:

| Repository | Receiver | Reconciles |
| --- | --- | --- |
| onek8s/aks-fluxcd-platform | `flux-system/github` | GitRepository `platform` |
| onek8s/aks-fluxcd-example | `ext-fi-example/github` (`tenants/ext-fi-example/webhook.yaml`) | GitRepository `ext-fi-example`, the team's |

notification-controller serves every Receiver on the same service, under
`/hook/sha256(token + name + namespace)`, so the one route above reaches both. Without the webhook,
Flux still polls the repository.

### Image signatures

Every image the environment's registry builds is signed by the platform (aks-terraform's **Sign
images** workflow, with the certificate `image-signing` in the platform vault), and AKS Image
Integrity checks each application pod's image at admission: Ratify, which AKS runs in
`gatekeeper-system`, answers the Azure Policy add-on's question "is this image signed with Notation"
for the assignment `k8s-image-integrity` (azure-landing-zone `stacks/aks-policy`). Image Integrity is
in preview and only audits, so an unsigned image is reported, not refused.

`infrastructure/image-verification` is Ratify's configuration: a `KeyManagementProvider` with the
certificate from the platform vault, a `Store` for the signatures in the registry and a Notation
`Verifier` that trusts signatures by that certificate's subject and no others. Ratify reads the vault
and the registry as the environment's shared identity `id-<region>-<env>-shared-ratify` (aks-terraform
`shared/`), through each cluster's federated credential for `gatekeeper-system/ratify-admin`; the
values come from `cluster-settings.yaml` (`image_verification_client_id`,
`image_signing_certificate_name`, `image_signing_subject`, `container_registry_login_server`).

### Platform gateway

`infrastructure/platform-gateway` is the Gateway `platform`, class `approuting-istio`, its Envoy
pods on the system pool behind a public load balancer, with one listener: `https-flux-webhook` for
`flux-system`.

The gateway namespaces enforce the baseline Pod Security Standard and audit and warn on restricted:
App Routing's gateway pods set no seccomp profile, and their security context is not on the
add-on's customization allow list. Everything else of restricted they meet.

[gatewayapi]: https://gateway-api.sigs.k8s.io/
[approutinggw]: https://learn.microsoft.com/azure/aks/app-routing-gateway-api

## Persistent volumes

AKS's own StorageClasses put a volume's disk in the cluster's node resource group, which AKS deletes
together with the cluster. `portable-disk` (`infrastructure/storage`) puts it in
`portable_disk_resource_group_name` instead - the resource group the environment's clusters and
their shared resources are in - so the disk outlives the cluster it was created in, and can be
attached to the next one:

- `StandardSSD_ZRS`: zone-redundant, so the disk attaches to a node in any zone of the region.
- `reclaimPolicy: Retain`: deleting the PVC, or the cluster, never deletes the disk. A disk nobody
  needs any more is deleted in Azure by hand.
- **The only storage in the users' namespaces.** Azure Policy - the platform's one policy engine,
  assigned by azure-landing-zone `stacks/aks-policy` - refuses a PersistentVolumeClaim on any other
  class (`portable-storage-only`, effect Deny) - or on none, which the API server would turn into
  AKS's `default` - and a pod with an inline `csi` volume. Every namespace is a user's but
  Kubernetes' and AKS's and the platform's own (`flux-system`, `external-secrets`, `external-dns` and
  the zones' and the platform's gateway and secrets namespaces), the assignment's excluded
  namespaces. Only creation is checked.
- Not the default class (AKS keeps `default` as that), so a claim names it:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
spec:
  storageClassName: portable-disk
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 8Gi
```

## Stateless clusters

aks-dev-stateless-az1 and aks-dev-stateless-az2 have no persistent storage at all. AKS runs none of its CSI drivers there - Azure
Disk, Azure Files, Azure Blob - nor the snapshot controller (aks-terraform,
`persistent_storage_enabled = false`), so the cluster has no StorageClass of AKS's and a claim has
nothing to be provisioned from. `clusters/dev-stateless-az<N>` leaves out the `storage` Kustomization - no
`portable-disk`, no `portable-storage-only` policy - and `portable_disk_resource_group_name`, which
aks-terraform does not send.

AKS's addon-manager still creates two StorageClasses, `default` and `managed-csi-premium-v2`, with
`addonmanager.kubernetes.io/mode: EnsureExists` - it creates them again if they are deleted. So Azure
Policy's `no-persistent-storage` (azure-landing-zone `stacks/aks-policy`, effect Deny, on the
stateless clusters only) refuses the creation of any StorageClass or PersistentVolume, by anyone, of
a PersistentVolumeClaim in any namespace but `kube-system`, and of a pod with an inline `csi` or
`ephemeral` volume. With it in place a cluster admin deletes AKS's two classes once, and the
addon-manager cannot bring them back:

```sh
kubectl delete storageclass default managed-csi-premium-v2
```

Only workloads that keep no state run there. The `example` team's Flux configuration syncs
`stateless/` of its repository on the stateless clusters rather than `apps/`: aks-hello, without
boot-log.

### Zone nodes

The stateless clusters run [node auto provisioning][nap] (NAP, the AKS-managed Karpenter;
aks-terraform, `node_auto_provisioning_enabled`) without any of AKS's default NodePools. Their
`node-pools` Kustomization applies `infrastructure/node-pools`: for each isolation zone one
`AKSNodeClass` and two `NodePool`s, as in the design's
[node pools](https://onek8s.github.io/docs/04-node-pools.html):

| NodePool | Capacity | Architecture | Taints | Weight | Limits (lab) |
| --- | --- | --- | --- | --- | --- |
| `intfi`, `intse`, `extfi`, `extse` | on-demand | amd64 and arm64 | `platform/zone=<zone>:NoSchedule` | 10 | 16 vCPU, 64 GiB |
| `intfispot`, `intsespot`, `extfispot`, `extsespot` | spot | amd64 and arm64 | the zone's, and `platform/capacity=spot:NoSchedule` | 50 | 8 vCPU, 32 GiB |

- Every node carries the label `platform/zone=<zone>`, runs Azure Linux with encryption at host, and
  is in the cell's availability zone and the zone's own subnet (`nap_subnet_id_<zone>`), behind the
  zone's NSG.
- The VM sizes come from an allow-list of SKU series with VNet encryption and encryption at host:
  `Ds_v5`, `Ds_v6`, `Es_v5`, `Es_v6` (amd64) and `Dps_v6`, `Eps_v6` (arm64).
- A pod that tolerates spot is tried on `<zone>spot` first, for its higher weight, and lands on
  `<zone>` when Azure has no spot capacity; one that does not stays off spot nodes, by their taint.
  The team chooses, per application: tolerate spot (and pick an architecture, or leave it open) and
  NAP uses whatever is available, cheapest first.
- Empty and under-used nodes are consolidated after 5 minutes, at most 10 % of a NodePool's nodes at
  a time, and every node is replaced after 30 days at the latest.

A team's pods need the zone's node selector and toleration (and the spot toleration to run on spot),
which the design injects with Azure Policy mutations that are not in place yet, so for now the team's
manifests say so themselves.

[nap]: https://learn.microsoft.com/en-us/azure/aks/node-autoprovision

## Tenants

A team's application is one entry in aks-terraform's `managed_namespaces`, `<zone>-<app>`: the AKS
managed namespace with its quota, default network policies (`AllowSameNamespace` both ways), Pod
Security Standard and the label `platform/zone`, who gets into it, its share of its zone's vault, and -
in `flux` - its repository. aks-terraform turns that last part into a **namespace-scoped Flux
configuration** of the team's own, named after the namespace. The extension applies it as the
`flux-applier` service account of that namespace and refuses references across namespaces, so the
team changes its own namespace and nothing else.

`tenants/<namespace>/` here is what the team needs inside the cluster: the `key-vault` service account
and SecretStore for the namespace's share of its zone's vault, and the `ExternalSecret`s and
Receiver the platform keeps for it. The zone's Cilium baseline applies by the namespace's label.

Adding a team is therefore an entry in aks-terraform's variables files (the cells' and `shared/`'s)
and a `tenants/<namespace>/` directory here, listed in `tenants/kustomization.yaml`.

| Team | Namespace | Repository |
| --- | --- | --- |
| example | `ext-fi-example` | [onek8s/aks-fluxcd-example](https://github.com/onek8s/aks-fluxcd-example) (`stateless/` on the stateless cells) |

A team keeps its own secrets as `<namespace>--<name>` in its zone's vault, and syncs them with an
`ExternalSecret` of its own against the namespace's `key-vault` store:

```sh
az keyvault secret set --vault-name kv-onek8s-dev-ext-fi --name ext-fi-example--api-key --value ...
```

The images need nothing: they are in the environment's Azure Container Registry (aks-terraform,
`shared/`: `container_registry_name`), and each cluster pulls them as its kubelet identity, which has
`AcrPull` there - no image pull secret.
