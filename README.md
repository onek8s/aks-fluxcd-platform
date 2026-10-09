# aks-fluxcd

What Flux deploys into the AKS clusters built by
[aks-terraform](https://github.com/olljanat-ai/aks-terraform). Terraform installs the AKS Flux
extension and a Flux configuration named `platform` that points at `clusters/<cluster>`; everything
else lives here.

## Layout

```
clusters/
  prototype-a/            # aks-prototype-a
    cluster-settings.yaml # this cluster's values, for the Kustomizations that need them
    infrastructure.yaml   # Flux Kustomizations `infra-controllers`, `infra-configs`,
                          # `infra-certificates`, `traefik`, `external-dns`, `flux-webhook`
                          # and `storage`
    tenants.yaml          # Flux Kustomization `tenants` -> tenants
  prototype-b/            # aks-prototype-b: the same, beside it on the same shared resources,
                          # but stateless - `stateless` instead of `storage` - see
                          # "Stateless clusters"
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
  flux-webhook/           # Flux's GitHub webhook receiver, on a public Gateway
  stateless/              # for a cluster without persistent storage: the admission policy that
                          # refuses every StorageClass, PersistentVolume and claim
  storage/                # the `portable-disk` StorageClass, whose disks outlive the cluster, and
                          # the admission policy that makes it the only storage users get
tenants/
  example/                # what the `example` team needs in its namespace; its repository,
                          # olljanat-ai/aks-fluxcd-example, is synced by aks-terraform's Flux
                          # configuration `example`
```

- `clusters/<cluster>/` is the path a cluster's `flux_git_repository.path` names. It holds Flux
  `Kustomization` objects only, each pulling in a shared directory, so adding a cluster is a new
  directory with the same files - and its own `cluster-settings.yaml`.
- `infrastructure/controllers/` is shared by every cluster. Its Kustomization waits for the
  controllers to be ready, so configuration that needs their CRDs can follow it with
  `dependsOn: [{ name: infra-controllers }]`.
- `infrastructure/configs/` follows `infra-controllers`, and `tenants/` and
  `infrastructure/certificates/` follow `infra-configs`, and `infrastructure/flux-webhook/` both. `infrastructure/traefik/`,
  `infrastructure/external-dns/` and `infrastructure/storage/` follow nothing. Nothing waits for Traefik, the certificates or
  external-dns, so a vault or a zone that is not set up yet holds back nothing but the Gateway's
  certificates and hostnames themselves.

The Kustomizations use `sourceRef: { kind: GitRepository, name: platform }` - the GitRepository the
AKS Flux extension creates for the Flux configuration Terraform names `platform`.

## Controllers

| Controller | Namespace | Chart |
| --- | --- | --- |
| External Secrets Operator | `external-secrets` | `external-secrets` 2.12.x from https://charts.external-secrets.io |
| external-dns | `external-dns` | `external-dns` 1.23.x from https://kubernetes-sigs.github.io/external-dns/, three times: `external-dns` (public zone), `external-dns-internal` (private zone) and `external-dns-public` (the webhook's public address) |
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
| `ingress-gateway` | `ingress-gateway--<gateway_certificate_name>` - a wildcard certificate for both zones | `gateway-tls`, the certificate of every HTTPS listener |
| `flux-system` | `flux-system--github-webhook-token` - created by aks-terraform | `github-webhook-token`, the token Flux's GitHub Receiver checks deliveries against |
| `example` | `example--aks-fluxcd-example-git` - a read-only token for the team's repository | `aks-fluxcd-example-git`, for Flux |

The team's images need no secret: they are in the environment's container registry, pulled as the
cluster's kubelet identity - see "Secrets a tenant needs".

The names of every secret can be listed by every identity with a share - only values are kept apart
- so no secret goes in a name. The cluster's admin groups run the whole vault; a team namespace's
`writer` and `admin` grants manage that namespace's secrets.

## Publishing applications

Applications are published through the [Kubernetes Gateway API][gatewayapi] on
[Traefik Proxy][traefik], on two Gateways: `internal`, reached through an
[Azure internal load balancer][ilb] at a private address in the cluster's network, for the private
zone's hostnames, and `public`, reached through a public IP, for the public zone's - this is a
prototype, and its apps and Traefik's dashboard are on the internet.
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
| The GatewayClass, Traefik and its internal and public load balancers | Platform | `infrastructure/traefik` |
| The `internal` and `public` Gateways: hostnames, listeners, who may attach to each, TLS, HTTP to HTTPS redirect | Platform | `infrastructure/configs/gateway` |
| Certificates: kept in Key Vault, synced into the listeners' Secrets | Platform | `infrastructure/certificates` |
| DNS: a record per listener hostname in Azure DNS or Azure Private DNS | Platform | `infrastructure/external-dns` |
| Letting Traefik into a team namespace | Platform | `tenants/<team>/networkpolicy.yaml` |
| `HTTPRoute`, `Service`, the app | Team | the team's own repository |

- **Traefik** runs one replica - the prototype cluster is a single small node; an update replaces
  the pod rather than surging a second one, so it needs no spare room - behind two `LoadBalancer`
  Services. `traefik` carries `service.beta.kubernetes.io/azure-load-balancer-internal: "true"`, so
  AKS puts its frontend on a private IP of the cluster's network, and carries the entry points `web`
  (8000) and `websecure` (8443). `traefik-public` gets a public IP and carries `web-pub` (9000)
  and `websecure-pub` (9443). An entry point is on one of them only, so a listener of one Gateway
  cannot be reached through the other's load balancer, whatever Host or SNI a client sends. Both
  serve 80 and 443 outside. `externalTrafficPolicy: Local` keeps the client's address.
- The **`internal` Gateway**, in `ingress-gateway`, on 8000 and 8443, has the private zone's
  hostnames:
  - an **`http`** listener, open to routes of `ingress-gateway` only, that redirects everything to
    HTTPS;
  - **`https-example-internal`**, open to the `HTTPRoute`s of the `example` namespace, and
    **`https-traefik-internal`**, open to `ingress-gateway`.
- The **`public` Gateway**, in `ingress-gateway`, on 9000 and 9443, has the public zone's:
  - an **`http`** listener that redirects to HTTPS, like the internal one;
  - **`https-example`** for the `example` namespace, **`https-traefik`** for `ingress-gateway`, and
    **`https-flux-webhook`** for `flux-system` - see [Webhook](#webhook).

  Every HTTPS listener terminates TLS with the wildcard certificate in `gateway-tls`. A team's route
  names the listeners it attaches to (`sectionName`) and neither hostname nor certificate: aks-hello
  attaches to `public`/`https-example` and `internal`/`https-example-internal`, and is published on
  both sides.

  `dashboard.yaml` serves Traefik's dashboard (`api@internal`) on `https-traefik` and
  `https-traefik-internal` **with no authentication**, so on the internet as well. It is read-only,
  but shows every router, service and backend; put it behind authentication, or take it off the
  public Gateway, before the cluster is more than a prototype. Traefik v3.6 lets a route in any
  namespace name its internal services; limiting that to `ingress-gateway`
  (`crossProviderNamespaces`) needs v3.7.1, which needs Kubernetes 1.36 - see below.

  Traefik requires a listener's port to be one of its entry points inside the pod; the load
  balancers serve them on 80 and 443.

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
- **DNS** (`infrastructure/external-dns`), three releases, each with a TXT record beside every
  record it keeps marking it as its own; none ever deletes a record it did not create:
  - `external-dns`, as `external-dns/external-dns`, keeps a record in the public zone for every
    hostname a Gateway in `ingress-gateway` serves there - the route's, or the listener's where the
    route names none. The public Gateway's `external-dns.kubernetes.io/target` annotation makes
    them CNAMEs to `public_hostname`; Traefik gives every Gateway the internal address in its status,
    so without it they would point inside the network.
  - `external-dns-public`, as the same service account, keeps `public_hostname` as an A record to
    the public IP of `traefik-public`. It reads Services in `traefik` only, so no team can publish a
    name of its own.
  - `external-dns-internal` keeps A records in the Azure Private DNS zone aks-terraform names
    (`internal_dns_zone_name`) to the internal Gateway's address, the internal load balancer's IP:
    the Azure provider writes public or private zones, never both. The private zone is a subdomain
    of the public one, so the public releases exclude it - its hostnames resolve only inside the
    networks it is linked to.

### Cluster settings

The values that differ by cluster are Flux variables, kept in `clusters/<cluster>/cluster-settings.yaml`
and handed to the Kustomizations through `postBuild.substituteFrom`:

| Variable | Set by | |
| --- | --- | --- |
| `key_vault_url`, `key_vault_client_id_<namespace>` | aks-terraform | The cluster's Key Vault, and the identity each namespace's `key-vault` service account reads its share of it as. A namespace added to the vault's shares in aks-terraform is added here too. |
| `dns_zone_name`, `dns_zone_resource_group_name`, `dns_zone_subscription_id`, `dns_identity_client_id` | aks-terraform | The zone the hostnames are published in, and the identity that writes it. |
| `internal_dns_zone_name`, `internal_dns_zone_resource_group_name`, `internal_dns_zone_subscription_id` | aks-terraform | The private zone the hostnames are also published in. |
| `cluster_name`, `azure_tenant_id` | aks-terraform | external-dns's record owner, and the tenant the identities live in. |
| `portable_disk_resource_group_name` | aks-terraform | Where the disks of the `portable-disk` StorageClass go: the resource group of the environment's clusters and shared resources, not the cluster's node resource group. See [Persistent volumes](#persistent-volumes). |
| `gateway_certificate_name` | this repository | The Key Vault certificate every HTTPS listener serves, without its `ingress-gateway--` prefix: a wildcard for `*.<dns_zone_name>` and `*.<internal_dns_zone_name>`, issued by aks-terraform's Renew Certificate workflow. |
| `example_hostname` | this repository | The hostname of the public `https-example` listener: one level below the zone's apex, which is all the wildcard covers. |
| `example_internal_hostname` | this repository | The hostname of the `https-example-internal` listener, the same way in the private zone. |
| `traefik_hostname` | this repository | The public hostname of Traefik's dashboard (`https-traefik`). |
| `traefik_internal_hostname` | this repository | The dashboard's hostname in the private zone (`https-traefik-internal`). |
| `public_hostname` | this repository | The A record of the public IP, in `dns_zone_name`: every public hostname is a CNAME to it. |
| `flux_webhook_hostname` | this repository | The public hostname of Flux's webhook receiver, one level below `dns_zone_name` - see [Webhook](#webhook). |

The ones aks-terraform sets are Azure facts and are never written here: it gives them to the
cluster's Flux configuration as post-build variables of the `platform` Kustomization, which fills
them into `cluster-settings.yaml` as it applies it.

A new hostname goes live in three steps: put a certificate covering it in the vault as
`ingress-gateway--<name>`, add a listener
for it to `infrastructure/configs/gateway/gateway.yaml` with an `ExternalSecret` beside the others,
and set both names in `cluster-settings.yaml`. external-dns publishes the record once a route
attaches to the listener.

### Webhook

A push to this repository reaches the cluster at once rather than at the next poll: GitHub calls
Flux's webhook receiver, which reconciles the `platform` GitRepository (`infrastructure/flux-webhook`).
Its route sends `/hook/` of `flux_webhook_hostname`, on the public Gateway's `https-flux-webhook`
listener, to notification-controller's `webhook-receiver`; nothing else of the receiver is routed.

- The `github` Receiver checks each delivery's signature against the token in Key Vault,
  `flux-system--github-webhook-token`, which aks-terraform creates (`flux_github_webhook`) and External
  Secrets syncs into `flux-system/github-webhook-token`.

The repository's webhook - Settings, Webhooks, Add webhook - is set once per cluster, each to the
cluster's own `flux_webhook_hostname` (`flux-webhook-a`, `flux-webhook-b`). The prototype clusters
read the same token from the shared vault, so their Receivers answer on the same path and both
webhooks have the same secret:

| | |
| --- | --- |
| Payload URL | `https://<flux_webhook_hostname><status.webhookPath of the Receiver>` - the path is printed by aks-terraform's Cluster status workflow (`kubectl get receiver -n flux-system github -o jsonpath='{.status.webhookPath}'`) |
| Content type | `application/json` |
| Secret | the token: `az keyvault secret show --vault-name <key vault> --name flux-system--github-webhook-token --query value -o tsv` |
| Events | Just the push event |

Without the webhook, or while it is down, Flux still polls the repository.

[ilb]: https://learn.microsoft.com/azure/aks/internal-lb
[gatewayapi]: https://gateway-api.sigs.k8s.io/
[roles]: https://gateway-api.sigs.k8s.io/concepts/roles-and-personas/
[traefik]: https://doc.traefik.io/traefik/reference/install-configuration/providers/kubernetes/kubernetes-gateway/

## Persistent volumes

AKS's own StorageClasses put a volume's disk in the cluster's node resource group, which AKS deletes
together with the cluster. `portable-disk` (`infrastructure/storage`) puts it in
`portable_disk_resource_group_name` instead - the resource group the environment's clusters and
their shared resources are in - so the disk outlives the cluster it was created in, and can be
attached to the next one:

- `StandardSSD_ZRS`: zone-redundant, so the disk attaches to a node in any zone of the region.
- `reclaimPolicy: Retain`: deleting the PVC, or the cluster, never deletes the disk. A disk nobody
  needs any more is deleted in Azure by hand.
- **The only storage in the users' namespaces.** The `portable-storage-only`
  ValidatingAdmissionPolicy refuses a PersistentVolumeClaim on any other class - or on none, which
  the API server would turn into AKS's `default` - and a pod with an inline `csi` volume. Every
  namespace is a user's but Kubernetes' and AKS's (`kube-*`) and the platform's own (`flux-system`,
  `external-secrets`, `external-dns`, `traefik`, `ingress-gateway`), listed in the binding in
  `infrastructure/storage/admission-policy.yaml`. Only creation is checked.
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

aks-prototype-b has no persistent storage at all. AKS runs none of its CSI drivers there - Azure
Disk, Azure Files, Azure Blob - nor the snapshot controller (aks-terraform,
`persistent_storage_enabled = false`), so the cluster has no StorageClass of AKS's and a claim has
nothing to be provisioned from. `clusters/prototype-b` leaves out the `storage` Kustomization - no
`portable-disk`, no `portable-storage-only` policy - and `portable_disk_resource_group_name`, which
aks-terraform does not send.

AKS's addon-manager still creates two StorageClasses, `default` and `managed-csi-premium-v2`, with
`addonmanager.kubernetes.io/mode: EnsureExists` - it creates them again if they are deleted. So the
cluster's `stateless` Kustomization applies `infrastructure/stateless`: the `no-persistent-storage`
ValidatingAdmissionPolicy refuses the creation of any StorageClass or PersistentVolume, by anyone,
of a PersistentVolumeClaim in any namespace but `kube-system`, and of a pod with an inline `csi` or
`ephemeral` volume. With it in place a cluster admin deletes AKS's two classes once, and the
addon-manager cannot bring them back:

```sh
kubectl delete storageclass default managed-csi-premium-v2
```

Only workloads that keep no state run there. The `example` team's Flux configuration syncs
`stateless/` of its repository on aks-prototype-b rather than `apps/`: aks-hello, without boot-log.

## Moving a workload to another cluster

The `example` team's `boot-log` app (olljanat-ai/aks-fluxcd-example) is a volume to try this on;
its README walks through moving it. The volume steps need a cluster with portable disks on both
ends - aks-prototype-b is [stateless](#stateless-clusters), so it takes stateless workloads only.

`clusters/prototype-a` (aks-prototype-a) and `clusters/prototype-b` (aks-prototype-b) are the
prototype environment's two clusters, on its shared Key Vault and DNS zones (aks-terraform,
`shared/`). Their platform hostnames are their own - `-a` and `-b` - and the `example` team runs in
both at once: on its stable name, `hello.onek8s.lol`, in aks-prototype-a, and as `hello-b` in
aks-prototype-b. Moving a workload from one to the other:

1. **Secrets** need nothing: the namespace is the same identity in both clusters -
   `id-<region>-<environment>-shared-kv-<namespace>`, with a federated credential per cluster for
   the namespace's `key-vault` service account - so it reads the same share of the vault with the
   same client ID, and keeps whatever else that identity has been granted.
2. **Volumes**: stop the workload in the old cluster, so the disk is detached - a `ReadWriteOnce`
   disk attaches to one node at a time. The PV's `spec.csi.volumeHandle` is the disk's resource ID.
   In the new cluster, bind the disk again with a PersistentVolume naming it, and a claim naming
   that volume:

   ```yaml
   apiVersion: v1
   kind: PersistentVolume
   metadata:
     name: example-data
   spec:
     capacity:
       storage: 8Gi
     accessModes: [ReadWriteOnce]
     persistentVolumeReclaimPolicy: Retain
     storageClassName: portable-disk
     csi:
       driver: disk.csi.azure.com
       volumeHandle: /subscriptions/<subscription>/resourceGroups/rg-aks-prototype/providers/Microsoft.Compute/disks/<disk>
   ---
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: data
     namespace: example
   spec:
     storageClassName: portable-disk
     volumeName: example-data
     accessModes: [ReadWriteOnce]
     resources:
       requests:
         storage: 8Gi
   ```

3. **Hostnames**: move the team's stable name. In the new cluster's `cluster-settings.yaml`, set
   `example_hostname` and `example_internal_hostname` to `hello.onek8s.lol` and
   `hello.internal.onek8s.lol`; in the old one's, to that cluster's own (`hello-a`, `hello-b`). The
   old cluster's external-dns deletes the records it owned, and the new one's publishes them; a
   record another cluster still owns is left alone until then.

## Tenants

A team is one entry in aks-terraform's `managed_namespaces`: the AKS managed namespace with its
quota, default network policy (closed ingress) and Pod Security Standard, who gets into it, its
share of the Key Vault, and - in `flux` - its repository. aks-terraform turns that last part into a
**namespace-scoped Flux configuration** of the team's own, named after the namespace, so whether the
team's repository is in sync shows on its own row in the portal's GitOps view of the cluster. The
extension applies it as the `flux-applier` service account of that namespace and refuses references
across namespaces, so the team changes its own namespace and nothing else.

`tenants/<team>/` here is what the team needs inside the cluster:

- a `key-vault` service account and SecretStore for the namespace's share of the Key Vault, and the
  `ExternalSecret`s for the secrets below - among them the token the namespace's Flux configuration
  reads the team's repository with;
- a `NetworkPolicy` that lets the Traefik pods in. The namespace accepts connections from its own
  pods only, and Traefik connects to the team's pods from the `traefik` namespace.

Adding a team is therefore an entry in aks-terraform's variables file and a `tenants/<team>/`
directory here, listed in `tenants/kustomization.yaml`.

`tenants/example/sync.yaml` and `rbac.yaml` are what synced the team's repository before: a
`GitRepository` and a `Kustomization` applying it as a `flux-reconciler` service account bound to
`admin`. They are kept suspended, and no longer pruning, until the namespace's Flux configuration is
Ready, and are deleted after that - see the comment in `sync.yaml`.

| Team | Namespace | Repository | Listener |
| --- | --- | --- | --- |
| example | `example` | [olljanat-ai/aks-fluxcd-example](https://github.com/olljanat-ai/aks-fluxcd-example) (`apps/` on aks-prototype-a, `stateless/` on aks-prototype-b) | `https-example`, `https-example-internal` |

### Secrets a tenant needs

The team's repository is private, and Git is no place for its credentials. The token Flux reads it
with is kept in the namespace's share of the Key Vault - by an admin, or by the team's own writers -
and synced from there. The name matters: the namespace's identity reads only the secrets under its
`example--` prefix.

```sh
# Read-only token (fine-grained, `Contents: read`) for the team's repository, for Flux.
az keyvault secret set --vault-name <key vault> --name example--aks-fluxcd-example-git --value github_pat_...
```

The images need nothing: they are in the environment's Azure Container Registry (aks-terraform,
`shared/`: `container_registry_name`), and each cluster pulls them as its kubelet identity, which has
`AcrPull` there - no image pull secret.

A team keeps its own secrets the same way, as `<namespace>--<name>`, and syncs them with an
`ExternalSecret` of its own against the namespace's `key-vault` store.
