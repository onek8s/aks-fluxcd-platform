# Node pools

The nodes of the isolation zones on a stateless cluster, provisioned by node auto provisioning
(NAP, the AKS-managed Karpenter) - OneK8s design section 4. aks-terraform turns NAP on
(node_auto_provisioning_enabled), with none of AKS's own default NodePools, so these are the only
NodePools the cluster has. A stateless cluster pulls this directory in through its `node-pools`
Flux Kustomization - see `clusters/dev-ext-stateless-az1/infrastructure.yaml` - on the directory of
its exposure, or per enabled country through a `node-pools-<zone>` Kustomization on the zone's own
directory (`internal/int-fi`, ...; `clusters/dev-int-stateless-az1/zones.yaml`, where Sweden is
disabled): internal and external workloads never share a cluster (OneK8s design decision D52),
so an internal cluster pulls in internal/ (int-fi, int-se) and an external one external/ (ext-fi,
ext-se). Each country's zone has its own nodes, subnet and vault inside the cluster of its exposure.

Every zone has one AKSNodeClass and two NodePools for its ordinary nodes, and a GPU AKSNodeClass
and NodePool:

  - `<zone>`, on-demand, and `<zone>spot`, on Spot VMs with the taint
    `platform/capacity=spot:NoSchedule` and a higher weight, so that NAP tries spot first for a pod
    that tolerates it and falls back to on-demand when Azure has no spot capacity.
  - Both amd64 and arm64: NAP picks the architecture from the pod's `kubernetes.io/arch` node
    selector, or the cheapest fitting size of either for a pod without one.
  - The VM sizes of an allow-list of SKU series that support VNet encryption and encryption at
    host: Ds_v5, Ds_v6, Es_v5, Es_v6 (amd64) and Dps_v6, Eps_v6 (arm64, Cobalt 100). Several
    series per architecture give the cell's one availability zone more places to find capacity.
  - The label `platform/zone=<zone>` and the taint `platform/zone=<zone>:NoSchedule` on every node,
    in the cell's availability zone (`nap_availability_zone`) and the zone's own node subnet
    (`nap_subnet_id_<zone>`), both from cluster-settings.yaml, which aks-terraform fills in.
  - Consolidation of empty and under-used nodes at most 10 % of the NodePool's nodes at a time,
    and every node replaced after 30 days at the latest.
  - `<zone>gpu`, its own class and NodePool (design decision D42): NVIDIA H100 v5 on-demand VMs
    (the only GPU VMs with VNet encryption), amd64, the taint `platform/accelerator=nvidia-gpu`,
    AKS's managed GPU stack (driver, device plugin, DCGM exporter). A pod gets a GPU node by asking
    for `nvidia.com/gpu` and carrying the label `platform/accelerator: nvidia-gpu`, which an Azure
    Policy mutation turns into the toleration. Empty for a minute, the node is removed.

A node is only created for a pending pod that tolerates a zone's taint - an application of the
zone. The zones' gateways and External Secrets Operators (infrastructure/zones) run on the system
pool, so a zone without applications has no nodes, and its NodePools scale back to zero when its
last application pod is gone (consolidation of the empty node). A team's pods get the zone's node
selector and toleration - and the spot and GPU tolerations for `platform/capacity: spot` and
`platform/accelerator: nvidia-gpu` - from the Azure Policy mutations of design section 9
(azure-landing-zone stacks/aks-policy).
