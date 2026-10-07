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
  traefik/                # Traefik Hub API Gateway, behind an Azure internal load balancer
  certificates/           # the listener certificates, synced from Key Vault
  external-dns/           # external-dns, publishing the listener hostnames in Azure DNS
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
  external-dns, so a license, a vault or a zone that is not set up yet holds back nothing but the
  Gateway and its hostnames themselves.

The Kustomizations use `sourceRef: { kind: GitRepository, name: platform }` - the GitRepository the
AKS Flux extension creates for the Flux configuration Terraform names `platform`.

## Controllers

| Controller | Namespace | Chart |
| --- | --- | --- |
| External Secrets Operator | `external-secrets` | `external-secrets` 2.12.x from https://charts.external-secrets.io |
| external-dns | `external-dns` | `external-dns` 1.23.x from https://kubernetes-sigs.github.io/external-dns/ |
| Traefik Hub API Gateway | `traefik` | `traefik` 41.6.x from https://traefik.github.io/charts |

External Secrets Operator runs with no Azure identity of its own. The one store it has,
`key-vault` in `ingress-gateway`, reads Key Vault as a service account federated with an identity
aks-terraform creates.

## Publishing applications

Applications are published through the [Kubernetes Gateway API][gatewayapi] on
[Traefik Hub API Gateway][traefikhub], reached through an [Azure internal load balancer][ilb]: the
Gateway has a private address in the cluster's network and nothing outside the network can reach it.
aks-terraform turns on the managed Gateway API CRDs; Traefik (`infrastructure/traefik`) provides the
`traefik` GatewayClass and runs the proxies as pods in the `traefik` namespace.

The split between the platform and the teams is the [Gateway API role model][roles]:

| | Who | Where |
| --- | --- | --- |
| The Gateway API CRDs | AKS / Platform | aks-terraform |
| The GatewayClass, Traefik and its internal load balancer | Platform | `infrastructure/traefik` |
| The `internal` Gateway: hostnames, listeners, who may attach to each, TLS, HTTP to HTTPS redirect | Platform | `infrastructure/configs/gateway` |
| Certificates: kept in Key Vault, synced into the listeners' Secrets | Platform | `infrastructure/certificates` |
| DNS: an A record per listener hostname in Azure DNS | Platform | `infrastructure/external-dns` |
| Letting Traefik into a team namespace | Platform | `tenants/<team>/networkpolicy.yaml` |
| `HTTPRoute`, `Service`, the app | Team | the team's own repository |

- **Traefik** runs two replicas behind a `LoadBalancer` Service carrying
  `service.beta.kubernetes.io/azure-load-balancer-internal: "true"`, so AKS puts its frontend on a
  private IP of the cluster's network - in the node subnet, for a cluster in an existing network -
  rather than on a public one. `externalTrafficPolicy: Local` keeps the client's address. Traefik
  copies the load balancer's address into the status of the Gateways it serves.
- The **`internal` Gateway**, in `ingress-gateway`, has:
  - an **`http`** listener, open to routes of `ingress-gateway` only, that redirects everything to
    HTTPS;
  - one **HTTPS listener per published hostname**, each open to the `HTTPRoute`s of one team
    namespace and terminating TLS with the certificate in the Secret it names. A team's
    route names the listener (`sectionName`) and neither hostname nor certificate.

  Traefik requires a listener's port to be one of its entry points inside the pod - `8000` (`web`)
  and `8443` (`websecure`) - which the load balancer serves on 80 and 443.

### Traefik Hub license

Traefik Hub API Gateway needs a license token, which Git is no place for. Until External Secrets has
a store to sync it from, it is created by hand, once per cluster:

```sh
kubectl -n traefik create secret generic traefik-hub-license --from-literal=token=<license token>
```

Until it exists the Traefik pods do not start and the `traefik` Kustomization stays not-ready; nothing
else waits for it.

### Certificates and DNS

Traefik reads listener certificates from Kubernetes Secrets, and has no DNS integration; AKS's Key
Vault and DNS automation serves the Istio based GatewayClasses only. So both run here, each acting as an identity aks-terraform creates and federates with one service
account:

- **Certificates** (`infrastructure/certificates`): the certificates are kept in Key Vault - imported
  by hand, or issued by the vault through an integrated CA. An `ExternalSecret` per listener reads
  the certificate's secret - a PFX - as `ingress-gateway/keyvault-certificates` and writes it as the
  `kubernetes.io/tls` Secret the listener names. A new version in the vault reaches the listener
  within the hour.
- **DNS** (`infrastructure/external-dns`): external-dns, as `external-dns/external-dns`, keeps an
  A record for every hostname served by a Gateway in `ingress-gateway` - the route's, or the
  listener's where the route names none - pointing at the Gateway's address, the private IP of the
  internal load balancer, with a TXT record beside it marking it as this cluster's. It only ever
  deletes records it created. The zone is the one aks-terraform names; a public zone then answers
  with a private address, which only resolves to something reachable from inside the network.

### Cluster settings

The values that differ by cluster are Flux variables, kept in `clusters/<cluster>/cluster-settings.yaml`
and handed to the Kustomizations through `postBuild.substituteFrom`:

| Variable | Set by | |
| --- | --- | --- |
| `tls_key_vault_url`, `tls_identity_client_id` | aks-terraform | The vault the certificates are synced from, and the identity that reads it. |
| `dns_zone_name`, `dns_zone_resource_group_name`, `dns_zone_subscription_id`, `dns_identity_client_id` | aks-terraform | The zone the hostnames are published in, and the identity that writes it. |
| `cluster_name`, `azure_tenant_id` | aks-terraform | external-dns's record owner, and the tenant the identities live in. |
| `example_hostname` | this repository | The hostname of the `https-example` listener: in the zone, and not its apex. |
| `example_certificate_name` | this repository | The Key Vault certificate the `https-example` listener serves. |

The ones aks-terraform sets are Azure facts and are never written here: it gives them to the
cluster's Flux configuration as post-build variables of the `platform` Kustomization, which fills
them into `cluster-settings.yaml` as it applies it.

A new hostname goes live in three steps: put a certificate covering it in the vault, add a listener
for it to `infrastructure/configs/gateway/gateway.yaml` with an `ExternalSecret` beside the others,
and set both names in `cluster-settings.yaml`. external-dns publishes the record once a route
attaches to the listener.

[ilb]: https://learn.microsoft.com/azure/aks/internal-lb
[gatewayapi]: https://gateway-api.sigs.k8s.io/
[roles]: https://gateway-api.sigs.k8s.io/concepts/roles-and-personas/
[traefikhub]: https://doc.traefik.io/traefik-hub/api-gateway/intro

## Tenants

A team namespace is an AKS managed namespace, created by aks-terraform with its quota, default
network policy (closed ingress) and Pod Security Standard. `tenants/<team>/` is what lets the team
deploy into it from its own repository:

- a `flux-reconciler` service account bound to the `admin` role in that namespace alone;
- a `GitRepository` for the team's repository and a Flux `Kustomization` that applies it as that
  service account, with `targetNamespace` set to the namespace. Both sit in the team namespace,
  since the AKS Flux extension refuses references across namespaces;
- a `NetworkPolicy` that lets the Traefik pods in. The namespace accepts connections from its own
  pods only, and Traefik connects to the team's pods from the `traefik` namespace.

The team can therefore change its own namespace and nothing else - its `HTTPRoute`s included, which
`infrastructure/configs/gateway-api-rbac.yaml` adds to `admin`.

| Team | Namespace | Repository | Listener |
| --- | --- | --- | --- |
| example | `example` | [olljanat-ai/aks-fluxcd-example](https://github.com/olljanat-ai/aks-fluxcd-example) (`apps/`) | `https-example` |

### Secrets a tenant needs

Team repositories are private, and Git is no place for their credentials. Until External Secrets has
a store to sync them from, they are created by hand:

```sh
# Read-only token (fine-grained, `Contents: read`) for the team's repository, for Flux.
kubectl -n example create secret generic aks-fluxcd-example-git \
  --from-literal=username=git --from-literal=password=github_pat_...

# Pull access to the team's image, while its GHCR package is private.
kubectl -n example create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=<github user> --docker-password=<token with read:packages>
```
