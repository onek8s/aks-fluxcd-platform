#!/usr/bin/env python3
"""Check an application's network policies against Azure Policy's `app-egress` rule (OneK8s design
sections 2.7 and 5.1): an application's policies stay inside the cluster, the hub firewall decides what
leaves it (design D80: Cilium at layers 3 and 4, no ACNS).

    check-egress.py manifests/...

Every YAML file under the given paths is read; for every CiliumNetworkPolicy (and Kubernetes
NetworkPolicy) found:

  - egress only to in-cluster endpoints (toEndpoints) and Services (toServices with k8sService /
    k8sServiceSelector) - no toCIDR, toCIDRSet, toEntities, toFQDNs, toGroups, toNodes, toRequires,
    and no rule without a peer (toPorts alone allows every destination);
  - no layer 7 rules (toPorts rules: DNS, HTTP, Kafka);
  - no Kubernetes NetworkPolicy ipBlock;
  - no CiliumClusterwideNetworkPolicy, CiliumCIDRGroup or CiliumEgressGatewayPolicy: platform only.

What an application may reach outside the cluster is its rule file in azure-landing-zone
(`firewall-applications/`). `--zone` and `--allow-lists` are accepted for older callers and ignored.
Exits 1 and prints every finding when anything is wrong.
"""
import argparse
import pathlib
import sys

import yaml

PLATFORM_ONLY = {"CiliumClusterwideNetworkPolicy", "CiliumCIDRGroup", "CiliumEgressGatewayPolicy"}
FORBIDDEN_EGRESS = ["toCIDR", "toCIDRSet", "toEntities", "toFQDNs", "toGroups", "toNodes", "toRequires"]
IN_CLUSTER = ["toEndpoints", "toServices"]


def check_policy(doc, where, findings):
    kind = doc.get("kind")
    name = f"{where}: {kind} {doc.get('metadata', {}).get('name')}"
    if kind in PLATFORM_ONLY:
        findings.append(f"{name}: {kind} is the platform's only")
        return
    if kind == "NetworkPolicy":
        for rule in doc.get("spec", {}).get("egress", []) or []:
            for peer in rule.get("to", []) or []:
                if "ipBlock" in peer:
                    findings.append(f"{name}: egress ipBlock is not allowed - the hub firewall decides what leaves the cluster")
        return
    if kind != "CiliumNetworkPolicy":
        return
    specs = doc.get("specs") or [doc.get("spec") or {}]
    for spec in specs:
        for rule in spec.get("egress", []) or []:
            for key in FORBIDDEN_EGRESS:
                if key in rule:
                    findings.append(f"{name}: egress {key} is not allowed - only toEndpoints and toServices; the hub firewall decides what leaves the cluster")
            if not any(key in rule for key in IN_CLUSTER + FORBIDDEN_EGRESS):
                findings.append(f"{name}: an egress rule without toEndpoints or toServices allows every destination")
            for svc in rule.get("toServices", []) or []:
                if not ("k8sService" in svc or "k8sServiceSelector" in svc):
                    findings.append(f"{name}: toServices must name an in-cluster Service")
            for port in rule.get("toPorts", []) or []:
                if port.get("rules"):
                    findings.append(f"{name}: layer 7 rules (toPorts rules) are not allowed - Cilium works at layers 3 and 4")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zone", help=argparse.SUPPRESS)
    parser.add_argument("--allow-lists", help=argparse.SUPPRESS)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    findings, checked = [], 0
    for root in args.paths:
        root = pathlib.Path(root)
        files = [root] if root.is_file() else sorted(list(root.rglob("*.yaml")) + list(root.rglob("*.yml")))
        for path in files:
            for doc in yaml.safe_load_all(path.read_text()):
                if isinstance(doc, dict) and doc.get("kind") in PLATFORM_ONLY | {"NetworkPolicy", "CiliumNetworkPolicy"}:
                    checked += 1
                    check_policy(doc, str(path), findings)
    for f in findings:
        print(f"::error::{f}")
    print(f"{checked} policies checked, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
