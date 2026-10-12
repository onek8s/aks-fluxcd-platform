#!/usr/bin/env python3
"""Check an application's egress policies against the rules of OneK8s design section 16.

    check-egress.py --zone ext-fi --allow-lists policy/egress-allow-lists.yaml manifests/...

Every YAML file under the given paths is read; for every CiliumNetworkPolicy (and Kubernetes
NetworkPolicy) found:

  - egress only to in-cluster endpoints (toEndpoints), Services (toServices with k8sService /
    k8sServiceSelector) and FQDNs (toFQDNs) with explicit toPorts - no toCIDR, toCIDRSet,
    toEntities, toGroups, toNodes, and no Kubernetes NetworkPolicy ipBlock;
  - every FQDN on the zone's allow-list; a pattern only as `*.<at least two labels>` and never a
    bare `*`;
  - DNS rules only for names the policy also allows in toFQDNs, plus the cluster's own names;
  - no Key Vault (`*.vault.azure.net`, `*.vaultcore.azure.net`) anywhere: applications get their
    secrets from the zone's External Secrets Operator;
  - no CiliumClusterwideNetworkPolicy, CiliumCIDRGroup or CiliumEgressGatewayPolicy: platform only.

Manifests may contain Flux post-build variables (`${...}`); they are left as they are. Exits 1 and
prints every finding when anything is wrong.
"""
import argparse
import fnmatch
import pathlib
import sys

import yaml

PLATFORM_ONLY = {"CiliumClusterwideNetworkPolicy", "CiliumCIDRGroup", "CiliumEgressGatewayPolicy"}
FORBIDDEN_EGRESS = ["toCIDR", "toCIDRSet", "toEntities", "toGroups", "toNodes", "toRequires"]
KEY_VAULT = ("vault.azure.net", "vaultcore.azure.net")
# Suffixes under which every customer has names: a wildcard right below one opens the whole service.
SHARED_SUFFIXES = (
    "core.windows.net", "blob.core.windows.net", "database.windows.net", "servicebus.windows.net",
    "azurewebsites.net", "cloudapp.azure.com", "cloudapp.net", "azurecr.io", "azure.com",
    "windows.net", "amazonaws.com", "googleapis.com", "github.io", "githubusercontent.com",
)


def cluster_name(name):
    return name.rstrip(".").endswith("cluster.local")


def allowed(name, allow):
    name = name.rstrip(".").lower()
    if name in allow.get("names", []):
        return True
    return any(fnmatch.fnmatchcase(name, p) for p in allow.get("patterns", []))


def check_policy(doc, where, allow, findings):
    kind = doc.get("kind")
    name = f"{where}: {kind} {doc.get('metadata', {}).get('name')}"
    if kind in PLATFORM_ONLY:
        findings.append(f"{name}: {kind} is the platform's only")
        return
    if kind == "NetworkPolicy":
        for rule in doc.get("spec", {}).get("egress", []) or []:
            for peer in rule.get("to", []) or []:
                if "ipBlock" in peer:
                    findings.append(f"{name}: egress ipBlock is not allowed - name the destination by FQDN in a CiliumNetworkPolicy")
        return
    if kind != "CiliumNetworkPolicy":
        return
    specs = doc.get("specs") or [doc.get("spec") or {}]
    for spec in specs:
        fqdns, dns_names = [], []
        for rule in spec.get("egress", []) or []:
            for key in FORBIDDEN_EGRESS:
                if key in rule:
                    findings.append(f"{name}: egress {key} is not allowed - only toEndpoints, toServices and toFQDNs")
            for svc in rule.get("toServices", []) or []:
                if not ("k8sService" in svc or "k8sServiceSelector" in svc):
                    findings.append(f"{name}: toServices must name an in-cluster Service")
            if "toFQDNs" in rule:
                if not rule.get("toPorts"):
                    findings.append(f"{name}: toFQDNs without toPorts - every destination names its ports")
                for f in rule["toFQDNs"]:
                    value = f.get("matchName") or f.get("matchPattern") or ""
                    fqdns.append(value)
            for port in rule.get("toPorts", []) or []:
                for d in ((port.get("rules") or {}).get("dns") or []):
                    dns_names.append(d.get("matchName") or d.get("matchPattern") or "")
        for value in fqdns:
            v = value.rstrip(".").lower()
            if v in ("*", "") or "*" in v.split(".", 1)[-1] or (v.startswith("*.") and (len(v.split(".")) < 4 or v[2:] in SHARED_SUFFIXES)):
                findings.append(f"{name}: toFQDNs {value!r}: a wildcard only as the leftmost label of a name at least two labels below a public suffix")
            if v.endswith(KEY_VAULT):
                findings.append(f"{name}: toFQDNs {value!r}: no Key Vault - secrets come from the zone's External Secrets Operator")
            elif "${" not in v and not allowed(v, allow):
                findings.append(f"{name}: toFQDNs {value!r} is not on the zone's allow-list (policy/egress-allow-lists.yaml)")
        for value in dns_names:
            v = value.rstrip(".").lower()
            if v == "*":
                findings.append(f"{name}: DNS rule '*' resolves anything - list the names the policy allows")
            elif v.endswith(KEY_VAULT):
                findings.append(f"{name}: DNS rule {value!r}: no Key Vault")
            elif not cluster_name(v) and v not in [f.rstrip(".").lower() for f in fqdns]:
                findings.append(f"{name}: DNS rule {value!r} for a name the policy does not allow in toFQDNs")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--allow-lists", required=True)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    lists = yaml.safe_load(pathlib.Path(args.allow_lists).read_text())["zones"]
    if args.zone not in lists:
        sys.exit(f"No allow-list for zone {args.zone} in {args.allow_lists}")
    allow = lists[args.zone]

    findings, checked = [], 0
    for root in args.paths:
        root = pathlib.Path(root)
        files = [root] if root.is_file() else sorted(list(root.rglob("*.yaml")) + list(root.rglob("*.yml")))
        for path in files:
            for doc in yaml.safe_load_all(path.read_text()):
                if isinstance(doc, dict) and doc.get("kind") in PLATFORM_ONLY | {"NetworkPolicy", "CiliumNetworkPolicy"}:
                    checked += 1
                    check_policy(doc, str(path), allow, findings)
    for f in findings:
        print(f"::error::{f}")
    print(f"{checked} policies checked against the {args.zone} zone, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
