# aks-fluxcd

What Flux deploys into the AKS clusters built by
[aks-terraform](https://github.com/olljanat-ai/aks-terraform). Terraform installs the AKS Flux
extension and a Flux configuration named `platform` that points at `clusters/<cluster>`; everything
else lives here.

## Layout

```
clusters/
  prototype/
    cluster-settings.yaml # this cluster's values, for the Kustomizations that need them
    infrastructure.yaml   # Flux Kustomizations `infra-controllers`, `infra-configs`,
                          # `infra-certificates`, `traefik` and `external-dns`
    tenants.yaml          # Flux Kustomization `tenants` -> tenants
infrastructure/
  controllers/            # cluster-wide controllers every cluster runs
    external-secrets/     # External Secrets Operator
  configs/                # configuration that needs the controllers' CRDs
    gateway-api-rbac.yaml # lets the namespace `admin`/`edit` roles manage HTTPRoutes
    gateway/              # the internal Gateway, the HTTP to HTTPS redirect
  traefik/                # Traefik Proxy, behind an Azure internal load balancer
  certificates/           # the listener certificates, synced from Key Vault
  external-dns/           # external-dns, publishing the listener hostnames in Azure DNS and
                          # Azure Private DNS
tenants/
  example/                # the `example` team: olljanat-ai/aks-fluxcd-example, synced into `example`
```

- `clusters/<cluster>/` is the path a cluster's `flux_git_repository.path` names. It holds Flux
  `Kustomization` objects only, each pulling in a shared directory, so adding a cluster is a new
  directory with the same files - and its own `cluster-settings.yaml`.
- `infrastructure/controllers/` is shared by every cluster. Its Kustomization waits for the
  controllers to be ready, so configuration that needs their CRDs can follow it with
  `dependsOn: [{ name: infra-controllers }]`.
- `infrastructure/configs/` follows `infra-controllers`, and `tenants/` and
  `infrastructure/certificates/` follow `infra-configs`. `infrastructure/traefik/` and
  `infrastructure/external-dns/` follow nothing. Nothing waits for Traefik, the certificates or
  external-dns, so a vault or a zone that is not set up yet holds back nothing but the Gateway's
  certificates and hostnames themselves.

The Kustomizations use `sourceRef: { kind: GitRepository, name: platform }` - the GitRepository the
AKS Flux extension creates for the Flux configuration Terraform names `platform`.

## Controllers

| Controller | Namespace | Chart |
| --- | --- | --- |
| External Secrets Operator | `external-secrets` | `external-secrets` 2.12.x from https://charts.external-secrets.io |
| external-dns | `external-dns` | `external-dns` 1.23.x from https://kubernetes-sigs.github.io/external-dns/, twice: `external-dns` (public zone) and `external-dns-internal` (private zone) |
| Traefik Proxy | `traefik` | `traefik` 39.0.x (Traefik v3.6) from https://traefik.github.io/charts |

The `HelmRelease` and `HelmRepository` objects live in `flux-system` and install into the namespace
in the table through `targetNamespace` and `storageNamespace` (the Helm release Secrets are kept
there too). The AKS Flux extension runs Flux multi-tenant: a HelmRelease or Kustomization without a
`serviceAccountName` is applied as the `flux-applier` service account of its own namespace, and only
`flux-system`'s exists, with cluster-wide rights. A release in its own namespace fails with
`secrets is forbidden: User "system:serviceaccount:<namespace>:flux-applier"`. The extension also
refuses references across namespaces, so the HelmRepository sits next to the HelmRelease.

External Secrets Operator runs with no Azure identity of its own. It reads the cluster's Key Vault
through a `key-vault` SecretStore in each namespace that uses it - see [Key Vault](#key-vault).

## Key Vault

aks-terraform creates one Key Vault for the cluster and gives every namespace its own share of it:
**a namespace reads the secrets named `<namespace>--<name>`, and no others**. The vault enforces that
itself, with an Azure ABAC condition on the role assignment of each namespace's identity, so nothing
in the cluster has to be trusted to keep the namespaces apart.

In each namespace that uses the vault there is a `key-vault` service account, federated with that
namespace's identity (`key_vault_client_id_<namespace>` in `cluster-settings.yaml`), and a
`key-vault` SecretStore naming it. External Secrets Operator requests a token for the service
account on every read, so an `ExternalSecret` in a namespace reads that namespace's secrets only.

| Namespace | Key Vault secret | Synced into |
| --- | --- | --- |
| `ingress-gateway` | `ingress-gateway--<example_certificate_name>` - a certificate | `https-example-tls`, the certificate of `https-example` and `https-example-internal` |
| `ingress-gateway` | `ingress-gateway--<traefik_certificate_name>` - a certificate | `https-traefik-tls`, the certificate of `https-traefik` and `https-traefik-internal` |
| `example` | `example--aks-fluxcd-example-git` - a read-only token for the team's repository | `aks-fluxcd-example-git`, for Flux |
| `example` | `example--ghcr-pull` - a Docker config JSON | `ghcr-pull`, for pulling the team's images |

The names of every secret can be listed by every identity with a share - only values are kept apart
- so no secret goes in a name. The cluster's admin groups run the whole vault; a team namespace's
`writer` and `admin` grants manage that namespace's secrets.

## Publishing applications

Applications are published through the [Kubernetes Gateway API][gatewayapi] on
[Traefik Proxy][traefik], reached through an [Azure internal load balancer][ilb]: the
Gateway has a private address in the cluster's network and nothing outside the network can reach it.
aks-terraform turns on the managed Gateway API CRDs, whose version AKS ties to the cluster's
Kubernetes version - and Traefik's minor version is tied to it in turn: Traefik serves no Gateway
until it can watch every Gateway API kind it knows, so v3.6 needs Gateway API v1.4 (Kubernetes 1.35)
and v3.7 needs v1.5 (Kubernetes 1.36). Traefik (`infrastructure/traefik`) provides the
`traefik` GatewayClass and runs the proxies as pods in the `traefik` namespace. Only its Kubernetes
Gateway API provider is on: the chart's own CRDs (IngressRoute, Middleware and the rest of Traefik's,
and Traefik Hub's) are not installed, and nothing here uses them.

The split between the platform and the teams is the [Gateway API role model][roles]:

| | Who | Where |
| --- | --- | --- |
| The Gateway API CRDs | AKS / Platform | aks-terraform |
| The GatewayClass, Traefik and its internal load balancer | Platform | `infrastructure/traefik` |
| The `internal` Gateway: hostnames, listeners, who may attach to each, TLS, HTTP to HTTPS redirect | Platform | `infrastructure/configs/gateway` |
| Certificates: kept in Key Vault, synced into the listeners' Secrets | Platform | `infrastructure/certificates` |
| DNS: an A record per listener hostname in Azure DNS or Azure Private DNS | Platform | `infrastructure/external-dns` |
| Letting Traefik into a team namespace | Platform | `tenants/<team>/networkpolicy.yaml` |
| `HTTPRoute`, `Service`, the app | Team | the team's own repository |

- **Traefik** runs one replica - the prototype cluster is a single small node; an update replaces
  the pod rather than surging a second one, so it needs no spare room - behind a `LoadBalancer` Service carrying
  `service.beta.kubernetes.io/azure-load-balancer-internal: "true"`, so AKS puts its frontend on a
  private IP of the cluster's network - in the node subnet, for a cluster in an existing network -
  rather than on a public one. `externalTrafficPolicy: Local` keeps the client's address. Traefik
  copies the load balancer's address into the status of the Gateways it serves.
- The **`internal` Gateway**, in `ingress-gateway`, has:
  - an **`http`** listener, open to routes of `ingress-gateway` only, that redirects everything to
    HTTPS;
  - one **HTTPS listener per published hostname**, each open to the `HTTPRoute`s of one team
    namespace and terminating TLS with the certificate in the Secret it names. A team's
    route names the listener (`sectionName`) and neither hostname nor certificate. A team can be
    published on a hostname in the private zone as well - `https-example-internal` - by attaching
    its route to both listeners. That listener serves the public hostname's certificate, which does
    not name it, so clients see a name mismatch there;
  - **`https-traefik`** and **`https-traefik-internal`** listeners (public and private zone), open
    to routes of `ingress-gateway` only, on which
    `dashboard.yaml` serves Traefik's dashboard (`api@internal`) **with no authentication** - it is
    read-only, and the Gateway is only reachable from inside the network. Traefik v3.6 lets a route
    in any namespace name its internal services; limiting that to `ingress-gateway`
    (`crossProviderNamespaces`) needs v3.7.1, which needs Kubernetes 1.36 - see below.

  Traefik requires a listener's port to be one of its entry points inside the pod - `8000` (`web`)
  and `8443` (`websecure`) - which the load balancer serves on 80 and 443.

### Certificates and DNS

Traefik reads listener certificates from Kubernetes Secrets, and has no DNS integration; AKS's Key
Vault and DNS automation serves the Istio based GatewayClasses only. So both run here, each acting as an identity aks-terraform creates and federates with one service
account:

- **Certificates** (`infrastructure/certificates`): the certificates are kept in Key Vault as
  `ingress-gateway--<name>`, ingress-gateway's share of it - imported by an admin
  (`az keyvault certificate import --vault-name <key vault> --name ingress-gateway--<name> --file ...`),
  or issued by the vault through an integrated CA. An `ExternalSecret` per listener reads the
  certificate's secret - a PFX - as `ingress-gateway/key-vault` and writes it as the
  `kubernetes.io/tls` Secret the listener names. A new version in the vault reaches the listener
  within the hour.
- **DNS** (`infrastructure/external-dns`): external-dns, as `external-dns/external-dns`, keeps an
  A record for every hostname served by a Gateway in `ingress-gateway` - the route's, or the
  listener's where the route names none - pointing at the Gateway's address, the private IP of the
  internal load balancer, with a TXT record beside it marking it as this cluster's. It only ever
  deletes records it created. The zone is the one aks-terraform names; a public zone then answers
  with a private address, which only resolves to something reachable from inside the network.
  A second release, `external-dns/external-dns-internal`, does the same in the Azure Private DNS
  zone aks-terraform names (`internal_dns_zone_name`), as the same identity: the Azure provider
  writes public or private zones, never both. The private zone is a subdomain of the public one, so
  the public release excludes it - its hostnames resolve only inside the networks it is linked to.

### Cluster settings

The values that differ by cluster are Flux variables, kept in `clusters/<cluster>/cluster-settings.yaml`
and handed to the Kustomizations through `postBuild.substituteFrom`:

| Variable | Set by | |
| --- | --- | --- |
| `key_vault_url`, `key_vault_client_id_<namespace>` | aks-terraform | The cluster's Key Vault, and the identity each namespace's `key-vault` service account reads its share of it as. A namespace added to the vault's shares in aks-terraform is added here too. |
| `dns_zone_name`, `dns_zone_resource_group_name`, `dns_zone_subscription_id`, `dns_identity_client_id` | aks-terraform | The zone the hostnames are published in, and the identity that writes it. |
| `internal_dns_zone_name`, `internal_dns_zone_resource_group_name`, `internal_dns_zone_subscription_id` | aks-terraform | The private zone the hostnames are also published in. |
| `cluster_name`, `azure_tenant_id` | aks-terraform | external-dns's record owner, and the tenant the identities live in. |
| `example_hostname` | this repository | The hostname of the `https-example` listener: in the zone, and not its apex. |
| `example_certificate_name` | this repository | The Key Vault certificate the `https-example` listener serves, without its `ingress-gateway--` prefix. |
| `example_internal_hostname` | this repository | The hostname of the `https-example-internal` listener: in the private zone, and not its apex. |
| `traefik_hostname`, `traefik_certificate_name` | this repository | The hostname of Traefik's dashboard, and the Key Vault certificate its `https-traefik` listener serves. |
| `traefik_internal_hostname` | this repository | The dashboard's hostname in the private zone (`https-traefik-internal`), served the same certificate. |

The ones aks-terraform sets are Azure facts and are never written here: it gives them to the
cluster's Flux configuration as post-build variables of the `platform` Kustomization, which fills
them into `cluster-settings.yaml` as it applies it.

A new hostname goes live in three steps: put a certificate covering it in the vault as
`ingress-gateway--<name>`, add a listener
for it to `infrastructure/configs/gateway/gateway.yaml` with an `ExternalSecret` beside the others,
and set both names in `cluster-settings.yaml`. external-dns publishes the record once a route
attaches to the listener.

[ilb]: https://learn.microsoft.com/azure/aks/internal-lb
[gatewayapi]: https://gateway-api.sigs.k8s.io/
[roles]: https://gateway-api.sigs.k8s.io/concepts/roles-and-personas/
[traefik]: https://doc.traefik.io/traefik/reference/install-configuration/providers/kubernetes/kubernetes-gateway/

## Tenants

A team namespace is an AKS managed namespace, created by aks-terraform with its quota, default
network policy (closed ingress) and Pod Security Standard. `tenants/<team>/` is what lets the team
deploy into it from its own repository:

- a `flux-reconciler` service account bound to the `admin` role in that namespace alone;
- a `GitRepository` for the team's repository and a Flux `Kustomization` that applies it as that
  service account, with `targetNamespace` set to the namespace. Both sit in the team namespace,
  since the AKS Flux extension refuses references across namespaces;
- a `key-vault` service account and SecretStore for the namespace's share of the Key Vault, and the
  `ExternalSecret`s for the secrets below;
- a `NetworkPolicy` that lets the Traefik pods in. The namespace accepts connections from its own
  pods only, and Traefik connects to the team's pods from the `traefik` namespace.

The team can therefore change its own namespace and nothing else - its `HTTPRoute`s included, which
`infrastructure/configs/gateway-api-rbac.yaml` adds to `admin`.

| Team | Namespace | Repository | Listener |
| --- | --- | --- | --- |
| example | `example` | [olljanat-ai/aks-fluxcd-example](https://github.com/olljanat-ai/aks-fluxcd-example) (`apps/`) | `https-example`, `https-example-internal` |

### Secrets a tenant needs

Team repositories and images are private, and Git is no place for their credentials. They are kept
in the namespace's share of the Key Vault - by an admin, or by the team's own writers - and synced
from there:

```sh
# Read-only token (fine-grained, `Contents: read`) for the team's repository, for Flux.
az keyvault secret set --vault-name <key vault> --name example--aks-fluxcd-example-git --value github_pat_...

# Pull access to the team's images, while their GHCR packages are private: a Docker config JSON.
az keyvault secret set --vault-name <key vault> --name example--ghcr-pull --value "$(
  kubectl create secret docker-registry ghcr-pull --dry-run=client -o jsonpath='{.data.\.dockerconfigjson}' \
    --docker-server=ghcr.io --docker-username=<github user> --docker-password=<token with read:packages> |
  base64 -d)"
```

A Secret created by hand before the vault was in use is taken over by its `ExternalSecret`; if
External Secrets reports that it cannot, delete the hand-made one and it is created from the vault.

A team keeps its own secrets the same way, as `<namespace>--<name>`, and syncs them with an
`ExternalSecret` of its own against the namespace's `key-vault` store.
