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
                          # `infra-certificates` and `external-dns`
    tenants.yaml          # Flux Kustomization `tenants` -> tenants
infrastructure/
  controllers/            # cluster-wide controllers every cluster runs
    external-secrets/     # External Secrets Operator
  configs/                # configuration that needs the controllers' CRDs
    gateway-api-rbac.yaml # lets the namespace `admin`/`edit` roles manage HTTPRoutes
    gateway/              # the AGC load balancer, the public Gateway, the HTTP to HTTPS redirect
  certificates/           # the listener certificates, synced from Key Vault
  external-dns/           # external-dns, publishing the listener hostnames in Azure DNS
tenants/
  example/                # the `example` team: olljanat-ai/aks-hello, synced into `example`
```

- `clusters/<cluster>/` is the path a cluster's `flux_git_repository.path` names. It holds Flux
  `Kustomization` objects only, each pulling in a shared directory, so adding a cluster is a new
  directory with the same files - and its own `cluster-settings.yaml`.
- `infrastructure/controllers/` is shared by every cluster. Its Kustomization waits for the
  controllers to be ready, so configuration that needs their CRDs can follow it with
  `dependsOn: [{ name: infra-controllers }]`.
- `infrastructure/configs/` follows `infra-controllers`, and `tenants/` and
  `infrastructure/certificates/` follow `infra-configs`. `infrastructure/external-dns/` follows
  nothing. Nothing waits for the certificates or for external-dns, so a vault or a zone that is not
  set up yet holds back nothing but the hostnames themselves.

The Kustomizations use `sourceRef: { kind: GitRepository, name: platform }` - the GitRepository the
AKS Flux extension creates for the Flux configuration Terraform names `platform`.

## Controllers

| Controller | Namespace | Chart |
| --- | --- | --- |
| External Secrets Operator | `external-secrets` | `external-secrets` 2.12.x from https://charts.external-secrets.io |
| external-dns | `external-dns` | `external-dns` 1.23.x from https://kubernetes-sigs.github.io/external-dns/ |

External Secrets Operator runs with no Azure identity of its own. The one store it has,
`key-vault` in `ingress-gateway`, reads Key Vault as a service account federated with an identity
aks-terraform creates.

## Publishing applications

Applications are published through the [Kubernetes Gateway API][gatewayapi] on
[Application Gateway for Containers][agc] (AGC), Azure's load balancer for it. aks-terraform turns on
the managed Gateway API CRDs and the ALB Controller add-on, which provides the `azure-alb-external`
GatewayClass; nothing here installs a gateway controller or CRDs of its own. The proxies are an
Azure resource, not pods in the cluster.

The split between the platform and the teams is the [Gateway API role model][roles]:

| | Who | Where |
| --- | --- | --- |
| The GatewayClass, its controller, the delegated subnet | AKS / Platform | aks-terraform |
| The AGC load balancer (`ApplicationLoadBalancer`) | Platform | `infrastructure/configs/gateway` |
| The `public` Gateway: hostnames, listeners, who may attach to each, TLS, HTTP to HTTPS redirect | Platform | `infrastructure/configs/gateway` |
| Certificates: kept in Key Vault, synced into the listeners' Secrets | Platform | `infrastructure/certificates` |
| DNS: a CNAME per listener hostname in Azure DNS | Platform | `infrastructure/external-dns` |
| Letting AGC into a team namespace | Platform | `tenants/<team>/networkpolicy.yaml` |
| `HTTPRoute`, `Service`, the app | Team | the team's own repository |

Everything lives in `ingress-gateway`:

- an **`ApplicationLoadBalancer`** named `public`. The add-on creates the AGC resource for it in the
  node resource group, joined to the delegated subnet, and deletes it again with the object;
- the **`public` Gateway**, a frontend of that load balancer. Its address is an Azure-generated FQDN
  (`<random>.fzNN.alb.azure.com`, in the Gateway's status) that external-dns publishes each
  hostname as a CNAME to. It has:
  - an **`http`** listener on port 80, open to routes of `ingress-gateway` only, that redirects
    everything to HTTPS;
  - one **HTTPS listener per published hostname**, each open to the `HTTPRoute`s of one team
    namespace and terminating TLS with the certificate in the Secret it names. A team's
    route names the listener (`sectionName`) and neither hostname nor certificate.

### Certificates and DNS

Application Gateway for Containers reads listener certificates from Kubernetes Secrets only, and has
no DNS integration; AKS's Key Vault and DNS automation serves the Istio based GatewayClasses only.
So both run here, each acting as an identity aks-terraform creates and federates with one service
account:

- **Certificates** (`infrastructure/certificates`): the certificates are kept in Key Vault - imported
  by hand, or issued by the vault through an integrated CA. An `ExternalSecret` per listener reads
  the certificate's secret - a PFX - as `ingress-gateway/keyvault-certificates` and writes it as the
  `kubernetes.io/tls` Secret the listener names. A new version in the vault reaches the listener
  within the hour.
- **DNS** (`infrastructure/external-dns`): external-dns, as `external-dns/external-dns`, keeps a
  CNAME for every hostname served by a Gateway in `ingress-gateway` - the route's, or the
  listener's where the route names none - pointing at the Gateway's address, with a TXT record
  beside it marking it as this cluster's. It only ever deletes records it created.

### Cluster settings

The values that differ by cluster are Flux variables, kept in `clusters/<cluster>/cluster-settings.yaml`
and handed to the Kustomizations through `postBuild.substituteFrom`:

| Variable | Set by | |
| --- | --- | --- |
| `agc_subnet_id`, `agc_subnet_cidr` | aks-terraform | The delegated AGC subnet, for the `ApplicationLoadBalancer` and the tenant `NetworkPolicy`s. |
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

[agc]: https://learn.microsoft.com/azure/application-gateway/for-containers/overview
[gatewayapi]: https://gateway-api.sigs.k8s.io/
[roles]: https://gateway-api.sigs.k8s.io/concepts/roles-and-personas/

## Tenants

A team namespace is an AKS managed namespace, created by aks-terraform with its quota, default
network policy (closed ingress) and Pod Security Standard. `tenants/<team>/` is what lets the team
deploy into it from its own repository:

- a `flux-reconciler` service account bound to the `admin` role in that namespace alone;
- a `GitRepository` for the team's repository and a Flux `Kustomization` that applies it as that
  service account, with `targetNamespace` set to the namespace. Both sit in the team namespace,
  since the AKS Flux extension refuses references across namespaces;
- a `NetworkPolicy` that lets AGC in from its subnet. The namespace accepts connections from its own
  pods only, and AGC reaches the pods from addresses in that subnet rather than from the cluster.

The team can therefore change its own namespace and nothing else - its `HTTPRoute`s included, which
`infrastructure/configs/gateway-api-rbac.yaml` adds to `admin`.

| Team | Namespace | Repository | Listener |
| --- | --- | --- | --- |
| example | `example` | [olljanat-ai/aks-hello](https://github.com/olljanat-ai/aks-hello) (`deploy/`) | `https-example` |

### Secrets a tenant needs

Team repositories are private, and Git is no place for their credentials. Until External Secrets has
a store to sync them from, they are created by hand:

```sh
# Read-only token (fine-grained, `Contents: read`) for the team's repository, for Flux.
kubectl -n example create secret generic aks-hello-git \
  --from-literal=username=git --from-literal=password=github_pat_...

# Pull access to the team's image, while its GHCR package is private.
kubectl -n example create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io --docker-username=<github user> --docker-password=<token with read:packages>
```
