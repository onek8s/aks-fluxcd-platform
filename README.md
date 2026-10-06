# aks-fluxcd

What Flux deploys into the AKS clusters built by
[aks-terraform](https://github.com/olljanat-ai/aks-terraform). Terraform installs the AKS Flux
extension and a Flux configuration named `platform` that points at `clusters/<cluster>`; everything
else lives here.

## Layout

```
clusters/
  prototype/
    infrastructure.yaml   # Flux Kustomizations `infra-controllers` and `infra-configs`
    tenants.yaml          # Flux Kustomization `tenants` -> tenants
infrastructure/
  controllers/            # cluster-wide controllers every cluster runs
    external-secrets/     # External Secrets Operator
    cert-manager/         # cert-manager, with its Gateway API integration on
  configs/                # configuration that needs the controllers' CRDs
    cluster-issuers.yaml  # Let's Encrypt, production and staging
    gateway-api-rbac.yaml # lets the namespace `admin`/`edit` roles manage HTTPRoutes
    gateway/              # the public Gateway, and the HTTP to HTTPS redirect
tenants/
  example/                # the `example` team: olljanat-ai/aks-hello, synced into `example`
```

- `clusters/<cluster>/` is the path a cluster's `flux_git_repository.path` names. It holds Flux
  `Kustomization` objects only, each pulling in a shared directory, so adding a cluster is a new
  directory with the same files - and its own values for the variables of `infra-configs`.
- `infrastructure/controllers/` is shared by every cluster. Its Kustomization waits for the
  controllers to be ready, so configuration that needs their CRDs can follow it with
  `dependsOn: [{ name: infra-controllers }]`.
- `infrastructure/configs/` follows `infra-controllers`, and `tenants/` follows `infra-configs`.

The Kustomizations use `sourceRef: { kind: GitRepository, name: platform }` - the GitRepository the
AKS Flux extension creates for the Flux configuration Terraform names `platform`.

## Controllers

| Controller | Namespace | Chart |
| --- | --- | --- |
| External Secrets Operator | `external-secrets` | `external-secrets` 2.12.x from https://charts.external-secrets.io |
| cert-manager | `cert-manager` | `cert-manager` v1.21.x from https://charts.jetstack.io |

External Secrets Operator is installed without any `SecretStore` or `ClusterSecretStore`, so it syncs
nothing until one is added.

## Publishing applications

Applications are published through the [Kubernetes Gateway API][gatewayapi], on the
[App Routing Gateway API implementation][approuting] AKS provides. aks-terraform turns on the
managed Gateway API CRDs and the `approuting-istio` GatewayClass; nothing here installs a gateway
controller or CRDs of its own.

The split between the platform and the teams is the [Gateway API role model][roles]:

| | Who | Where |
| --- | --- | --- |
| The GatewayClass and its control plane | AKS | aks-terraform |
| The `public` Gateway: hostnames, listeners, who may attach to each, TLS, HTTP to HTTPS redirect | Platform | `infrastructure/configs/gateway` |
| Certificates | Platform | cert-manager and `infrastructure/configs/cluster-issuers.yaml` |
| `HTTPRoute`, `Service`, the app | Team | the team's own repository |

The `public` Gateway lives in `ingress-gateway`. AKS gives it an Envoy `Deployment` (two to five
replicas through an HPA), a PDB and a `LoadBalancer` `Service` with a public IP there. It has:

- an **`http`** listener on port 80, open to routes of `ingress-gateway` only. It carries the
  ACME HTTP-01 challenges of cert-manager and redirects everything else to HTTPS;
- one **HTTPS listener per published hostname**, each open to the `HTTPRoute`s of one team
  namespace and terminating TLS with a certificate cert-manager keeps for that hostname. A team's
  route names the listener (`sectionName`) and neither hostname nor certificate.

The values that differ by cluster are Flux variables, set in `clusters/<cluster>/infrastructure.yaml`:

| Variable | prototype | |
| --- | --- | --- |
| `public_dns_label` | `olljanat-aks-prototype` | Azure DNS label of the Gateway's public IP: `<label>.<region>.cloudapp.azure.com`. Unique within the region. |
| `public_hostname` | `olljanat-aks-prototype.swedencentral.cloudapp.azure.com` | The hostname of the `https-example` listener. |

The prototype has no domain of its own, so the `example` team is published on the Azure name of the
public IP itself. With a domain, point a DNS record for each hostname at the Gateway's address and
set the listener hostnames to them; the certificates follow on their own.

AKS can also take listener certificates from Azure Key Vault and publish hostnames to Azure DNS,
through the App Routing operator. That needs App Routing itself, the Key Vault provider for the
Secrets Store CSI driver, a DNS zone and a workload identity, none of which the clusters have, so
certificates come from Let's Encrypt through cert-manager instead. Moving to Key Vault later changes
the listener's `tls` and nothing a team owns.

[approuting]: https://learn.microsoft.com/azure/aks/app-routing-gateway-api
[gatewayapi]: https://gateway-api.sigs.k8s.io/
[roles]: https://gateway-api.sigs.k8s.io/concepts/roles-and-personas/

## Tenants

A team namespace is an AKS managed namespace, created by aks-terraform with its quota, default
network policy (closed ingress) and Pod Security Standard. `tenants/<team>/` is what lets the team
deploy into it from its own repository:

- a `flux-reconciler` service account bound to the `admin` role in that namespace alone;
- a `GitRepository` for the team's repository and a Flux `Kustomization` that applies it as that
  service account, with `targetNamespace` set to the namespace. Both sit in the team namespace,
  since the AKS Flux extension refuses references across namespaces.

The team can therefore change its own namespace and nothing else - its `HTTPRoute`s included, which
`infrastructure/configs/gateway-api-rbac.yaml` adds to `admin`. Because the namespace accepts
connections from its own pods only, the team's repository carries a `NetworkPolicy` that lets the
Gateway's proxies in from `ingress-gateway`.

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
