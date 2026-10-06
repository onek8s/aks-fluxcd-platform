# aks-fluxcd

What Flux deploys into the AKS clusters built by
[aks-terraform](https://github.com/olljanat-ai/aks-terraform). Terraform installs the AKS Flux
extension and a Flux configuration named `main` that points at `clusters/<cluster>`; everything else
lives here.

## Layout

```
clusters/
  prototype/
    infrastructure.yaml   # Flux Kustomization `infra-controllers` -> infrastructure/controllers
infrastructure/
  controllers/            # cluster-wide controllers every cluster runs
    external-secrets/     # External Secrets Operator
```

- `clusters/<cluster>/` is the path a cluster's `flux_git_repository.path` names. It holds Flux
  `Kustomization` objects only, each pulling in a shared directory, so adding a cluster is a new
  directory with the same files.
- `infrastructure/controllers/` is shared by every cluster. Its Kustomization waits for the
  controllers to be ready, so configuration that needs their CRDs can follow it with
  `dependsOn: [{ name: infra-controllers }]`.

The Kustomizations use `sourceRef: { kind: GitRepository, name: main }` - the GitRepository the AKS
Flux extension creates for the Flux configuration Terraform names `main`.

## Controllers

| Controller | Namespace | Chart |
| --- | --- | --- |
| External Secrets Operator | `external-secrets` | `external-secrets` 2.12.x from https://charts.external-secrets.io |

External Secrets Operator is installed without any `SecretStore` or `ClusterSecretStore`, so it syncs
nothing until one is added.
