# aks-fluxcd

What Flux deploys into the AKS clusters built by
[aks-terraform](https://github.com/olljanat-ai/aks-terraform). Terraform installs the AKS Flux
extension and a Flux configuration named `platform` that points at `clusters/<cluster>`; everything
else lives here.

## Layout

```
clusters/
  prototype/
    cluster-settings.yaml # this cluster's values, for `infra-configs` and `tenants`
    infrastructure.yaml   # Flux Kustomizations `infra-controllers` and `infra-configs`
    tenants.yaml          # Flux Kustomization `tenants` -> tenants
infrastructure/
  controllers/            # cluster-wide controllers every cluster runs
    external-secrets/     # External Secrets Operator
    cert-manager/         # cert-manager, with its Gateway API integration on
  configs/                # configuration that needs the controllers' CRDs
    cluster-issuers.yaml  # Let's Encrypt, production and staging
    gateway-api-rbac.yaml # lets the namespace `admin`/`edit` roles manage HTTPRoutes
    gateway/              # the AGC load balancer, the public Gateway, the HTTP to HTTPS redirect
tenants/
  example/                # the `example` team: olljanat-ai/aks-hello, synced into `example`
```

- `clusters/<cluster>/` is the path a cluster's `flux_git_repository.path` names. It holds Flux
  `Kustomization` objects only, each pulling in a shared directory, so adding a cluster is a new
  directory with the same files - and its own `cluster-settings.yaml`.
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
| Certificates | Platform | cert-manager and `infrastructure/configs/cluster-issuers.yaml` |
| Letting AGC into a team namespace | Platform | `tenants/<team>/networkpolicy.yaml` |
| `HTTPRoute`, `Service`, the app | Team | the team's own repository |

Everything lives in `ingress-gateway`:

- an **`ApplicationLoadBalancer`** named `public`. The add-on creates the AGC resource for it in the
  node resource group, joined to the delegated subnet, and deletes it again with the object;
- the **`public` Gateway**, a frontend of that load balancer. Its address is an Azure-generated FQDN
  (`<random>.fzNN.alb.azure.com`, in the Gateway's status) that the published hostnames are CNAMEs
  to. It has:
  - an **`http`** listener on port 80, open to routes of `ingress-gateway` only. It carries the
    ACME HTTP-01 challenges of cert-manager and redirects everything else to HTTPS;
  - one **HTTPS listener per published hostname**, each open to the `HTTPRoute`s of one team
    namespace and terminating TLS with a certificate cert-manager keeps for that hostname. A team's
    route names the listener (`sectionName`) and neither hostname nor certificate.

### Cluster settings

The values that differ by cluster are Flux variables, kept in `clusters/<cluster>/cluster-settings.yaml`
and handed to `infra-configs` and `tenants` through `postBuild.substituteFrom`:

| Variable | Set by | |
| --- | --- | --- |
| `agc_subnet_id` | aks-terraform | Resource ID of the delegated AGC subnet, for the `ApplicationLoadBalancer`. |
| `agc_subnet_cidr` | aks-terraform | Its range, which AGC connects to the pods from. |
| `example_hostname` | this repository | The hostname of the `https-example` listener. |

The first two are Azure facts and are never written here: aks-terraform gives them to the cluster's
Flux configuration as post-build variables of the `platform` Kustomization, which fills them into
`cluster-settings.yaml` as it applies it.

A new hostname goes live in three steps: set it in `cluster-settings.yaml`, point a CNAME for it at
the Gateway's FQDN (`kubectl -n ingress-gateway get gateway public`), and wait for cert-manager to
have Let's Encrypt validate it over HTTP - which only succeeds once the CNAME resolves.

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
