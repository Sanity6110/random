#!/usr/bin/env python3
"""
Compare what Terraform thinks it manages against what actually exists in Azure.

Terraform only knows about resources in its state file. Anything created by
hand in the portal, by another pipeline, or left behind from manual testing
is invisible to it. This script pulls the real Azure resource IDs and the
Terraform-managed IDs, then diffs the two sets so you can see:

  - unmanaged_in_azure : exists in Azure but Terraform doesn't manage it
  - missing_from_azure : Terraform manages it but it no longer exists in Azure
  - managed_and_present: in both (the "boring", expected case)

Azure Resource Manager IDs (e.g. "/subscriptions/xxx/resourceGroups/rg/...")
are used as the join key because every azurerm_* resource exposes its ARM ID
as `id`, so no per-resource-type mapping is needed. IDs are compared
case-insensitively, since ARM treats the ID path segments (other than
resource names you chose yourself) as case-insensitive.

Data sources:
  Terraform side: `terraform show -json` output (either produced by this
  script via --run-terraform, or supplied via --state-json for offline use).

  Azure side: `az resource list` (all typed resources) plus `az group list`
  (resource groups themselves, which `az resource list` omits), either run
  directly via --run-az or supplied via --azure-resources-json /
  --azure-groups-json.

Usage:
    # Live, against whatever `az` and `terraform` are logged into/initialized for:
    python3 compare_terraform_azure.py --run-terraform --run-az

    # Offline / CI, from files you (or a pipeline stage) already dumped:
    terraform show -json > tf.json
    az resource list -o json > resources.json
    az group list -o json > groups.json
    python3 compare_terraform_azure.py \
        --state-json tf.json \
        --azure-resources-json resources.json \
        --azure-groups-json groups.json

    # Scope the Azure side to specific resource groups only:
    python3 compare_terraform_azure.py --run-terraform --run-az -g RG1 -g RG2

    # Only compare resources tagged NRF_Region=EMEA, and export the diff to CSV:
    python3 compare_terraform_azure.py --run-terraform --run-az \
        --tag NRF_Region=EMEA --csv diff.csv
"""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

# azurerm resource types whose `id` is not something `az resource list` /
# `az group list` will ever return (they're sub-resources of the ARM control
# plane, not standalone resources: role assignments, locks, policy
# assignments, etc). Flagging these as "missing from Azure" would just be
# noise, so they're excluded from that half of the diff by default.
NON_LISTABLE_TYPE_PREFIXES = (
    "azurerm_role_assignment",
    "azurerm_role_definition",
    "azurerm_management_lock",
    "azurerm_policy_assignment",
    "azurerm_policy_definition",
    "azurerm_monitor_diagnostic_setting",
    "azurerm_resource_group_template_deployment",
)


def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit(f"error: '{cmd[0]}' not found on PATH (needed to run: {' '.join(cmd)})")
    if result.returncode != 0:
        sys.exit(f"error: command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout


def iter_state_resources(module):
    for resource in module.get("resources", []):
        yield resource
    for child in module.get("child_modules", []):
        yield from iter_state_resources(child)


def get_terraform_resources(state_json):
    root_module = state_json.get("values", {}).get("root_module", {})
    resources = {}
    for resource in iter_state_resources(root_module):
        if resource.get("mode") != "managed":
            continue
        if "azurerm" not in resource.get("provider_name", ""):
            continue
        resource_id = resource.get("values", {}).get("id")
        if not resource_id or not resource_id.startswith("/subscriptions/"):
            continue
        resources[resource_id.lower()] = {
            "id": resource_id,
            "address": resource.get("address"),
            "type": resource.get("type"),
            "name": resource.get("name"),
            "tags": resource.get("values", {}).get("tags") or {},
            "listable": not resource.get("type", "").startswith(NON_LISTABLE_TYPE_PREFIXES),
        }
    return resources


def get_azure_resources(resource_list, group_list):
    resources = {}
    for item in group_list:
        resource_id = item.get("id")
        if resource_id:
            resources[resource_id.lower()] = {
                "id": resource_id,
                "type": "Microsoft.Resources/resourceGroups",
                "name": item.get("name"),
                "tags": item.get("tags") or {},
            }
    for item in resource_list:
        resource_id = item.get("id")
        if resource_id:
            resources[resource_id.lower()] = {
                "id": resource_id,
                "type": item.get("type"),
                "name": item.get("name"),
                "tags": item.get("tags") or {},
            }
    return resources


def matches_tags(tags, tag_filters):
    tags = tags or {}
    return all(tags.get(k) == v for k, v in tag_filters.items())


def filter_by_tags(resources, tag_filters):
    if not tag_filters:
        return resources
    return {i: r for i, r in resources.items() if matches_tags(r.get("tags"), tag_filters)}


def tags_to_str(tags):
    return ";".join(f"{k}={v}" for k, v in sorted((tags or {}).items()))


def resource_group_of(resource_id: str):
    parts = resource_id.lower().split("/")
    if "resourcegroups" in parts:
        idx = parts.index("resourcegroups")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def diff(terraform_resources, azure_resources):
    tf_ids = set(terraform_resources)
    az_ids = set(azure_resources)

    unmanaged_in_azure = sorted(
        (azure_resources[i] for i in (az_ids - tf_ids)),
        key=lambda r: r["id"],
    )

    missing_from_azure = sorted(
        (terraform_resources[i] for i in (tf_ids - az_ids) if terraform_resources[i]["listable"]),
        key=lambda r: r["id"],
    )

    managed_and_present = sorted(tf_ids & az_ids)

    return {
        "unmanaged_in_azure": unmanaged_in_azure,
        "missing_from_azure": missing_from_azure,
        "managed_and_present_count": len(managed_and_present),
    }


def print_summary(result):
    print(f"Managed and present in Azure : {result['managed_and_present_count']}")
    print(f"Unmanaged in Azure            : {len(result['unmanaged_in_azure'])}")
    print(f"Missing from Azure (drift)    : {len(result['missing_from_azure'])}")

    if result["unmanaged_in_azure"]:
        print("\nResources in Azure with no Terraform equivalent:")
        by_rg = {}
        for r in result["unmanaged_in_azure"]:
            by_rg.setdefault(resource_group_of(r["id"]) or "(no rg)", []).append(r)
        for rg, items in sorted(by_rg.items()):
            print(f"  {rg}:")
            for r in items:
                print(f"    {r['type']:<45} {r['name']}")

    if result["missing_from_azure"]:
        print("\nTerraform-managed resources missing from Azure (state drift):")
        for r in result["missing_from_azure"]:
            print(f"    {r['address']:<50} {r['id']}")


def write_csv(result, path):
    fieldnames = ["status", "id", "type", "name", "resource_group", "terraform_address", "tags"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in result["unmanaged_in_azure"]:
            writer.writerow({
                "status": "unmanaged_in_azure",
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "resource_group": resource_group_of(r["id"]) or "",
                "terraform_address": "",
                "tags": tags_to_str(r.get("tags")),
            })
        for r in result["missing_from_azure"]:
            writer.writerow({
                "status": "missing_from_azure",
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "resource_group": resource_group_of(r["id"]) or "",
                "terraform_address": r["address"],
                "tags": tags_to_str(r.get("tags")),
            })


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-terraform", action="store_true", help="Run `terraform show -json` directly")
    parser.add_argument("--run-az", action="store_true", help="Run `az resource list` / `az group list` directly")
    parser.add_argument("-g", "--resource-group", action="append", dest="resource_groups",
                         help="Scope the Azure side to this resource group (repeatable). Default: whole subscription.")
    parser.add_argument("--tag", action="append", dest="tags", metavar="KEY=VALUE",
                         help="Only compare resources carrying this tag (repeatable; all given tags must match). "
                              "E.g. --tag NRF_Region=EMEA")
    parser.add_argument("--state-json", help="Path to a pre-generated `terraform show -json` file")
    parser.add_argument("--azure-resources-json", help="Path to a pre-generated `az resource list -o json` file")
    parser.add_argument("--azure-groups-json", help="Path to a pre-generated `az group list -o json` file")
    parser.add_argument("-o", "--output", help="Write full JSON diff to this file in addition to the summary")
    parser.add_argument("--csv", help="Write the diff (unmanaged + missing) to this CSV file")
    args = parser.parse_args()

    tag_filters = {}
    for t in args.tags or []:
        if "=" not in t:
            parser.error(f"--tag must be KEY=VALUE, got: {t}")
        key, value = t.split("=", 1)
        tag_filters[key] = value

    if args.run_terraform:
        state_json = json.loads(run_cmd(["terraform", "show", "-json"]))
    elif args.state_json:
        state_json = json.loads(Path(args.state_json).read_text())
    else:
        parser.error("need --run-terraform or --state-json")

    if args.run_az:
        if args.resource_groups:
            resource_list = []
            group_list = []
            for rg in args.resource_groups:
                resource_list.extend(json.loads(run_cmd(["az", "resource", "list", "-g", rg, "-o", "json"])))
                group_list.append(json.loads(run_cmd(["az", "group", "show", "-n", rg, "-o", "json"])))
        else:
            resource_list = json.loads(run_cmd(["az", "resource", "list", "-o", "json"]))
            group_list = json.loads(run_cmd(["az", "group", "list", "-o", "json"]))
    elif args.azure_resources_json and args.azure_groups_json:
        resource_list = json.loads(Path(args.azure_resources_json).read_text())
        group_list = json.loads(Path(args.azure_groups_json).read_text())
    else:
        parser.error("need --run-az, or both --azure-resources-json and --azure-groups-json")

    terraform_resources = get_terraform_resources(state_json)
    azure_resources = get_azure_resources(resource_list, group_list)

    if args.resource_groups and not args.run_az:
        wanted = {rg.lower() for rg in args.resource_groups}
        terraform_resources = {
            i: r for i, r in terraform_resources.items() if resource_group_of(r["id"]) in wanted
        }
        azure_resources = {
            i: r for i, r in azure_resources.items() if resource_group_of(r["id"]) in wanted
        }

    if tag_filters:
        terraform_resources = filter_by_tags(terraform_resources, tag_filters)
        azure_resources = filter_by_tags(azure_resources, tag_filters)

    result = diff(terraform_resources, azure_resources)

    if tag_filters:
        tag_desc = ", ".join(f"{k}={v}" for k, v in tag_filters.items())
        print(f"Filtered to tags: {tag_desc}\n")

    print_summary(result)

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
        print(f"\nFull diff written to {args.output}")

    if args.csv:
        write_csv(result, args.csv)
        print(f"CSV diff written to {args.csv}")

    if result["unmanaged_in_azure"] or result["missing_from_azure"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
