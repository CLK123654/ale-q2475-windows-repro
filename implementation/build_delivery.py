from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "README.md",
    "current/payment-service.yaml",
    "release/backend-addresses.csv",
    "release/publish-policy.json",
}


def documents(path: Path) -> list[dict]:
    return [value for value in yaml.safe_load_all(path.read_text(encoding="utf-8")) if value]


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def validate_inputs(input_dir: Path) -> tuple[list[dict], list[dict], dict]:
    actual = {path.relative_to(input_dir).as_posix() for path in input_dir.rglob("*") if path.is_file()}
    if actual != EXPECTED:
        raise ValueError("input file set differs")
    current = documents(input_dir / "current/payment-service.yaml")
    with (input_dir / "release/backend-addresses.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_header = ["backend_id", "address_family", "address", "port", "ready", "zone"]
        if reader.fieldnames != expected_header:
            raise ValueError("backend address header differs")
        rows = list(reader)
    if not rows or len({row["backend_id"] for row in rows}) != len(rows):
        raise ValueError("backend identity differs")
    for row in rows:
        if row["address_family"] not in {"IPv4", "IPv6"} or row["ready"] not in {"true", "false"}:
            raise ValueError("backend field differs")
        address = ipaddress.ip_address(row["address"])
        if (address.version == 4) != (row["address_family"] == "IPv4"):
            raise ValueError("backend family differs")
        if not 1 <= int(row["port"]) <= 65535:
            raise ValueError("backend port differs")
    policy = json.loads((input_dir / "release/publish-policy.json").read_text(encoding="utf-8"))
    required = {
        "namespace", "legacy_service", "new_service", "new_service_type", "ip_family_policy", "ip_families",
        "service_port_name", "service_port", "protocol", "slice_label", "apply_order", "post_apply_owner",
        "post_apply_checks",
    }
    if set(policy) != required or policy["new_service_type"] != "HEADLESS_NO_SELECTOR":
        raise ValueError("publish policy differs")
    return current, rows, policy


def find(items: list[dict], kind: str, name: str) -> dict:
    return next(value for value in items if value.get("kind") == kind and value.get("metadata", {}).get("name") == name)


def service_signature(value: dict) -> dict:
    spec = value.get("spec", {})
    return {
        "name": value.get("metadata", {}).get("name"),
        "namespace": value.get("metadata", {}).get("namespace"),
        "clusterIP": spec.get("clusterIP"),
        "clusterIPs": spec.get("clusterIPs", []),
        "ipFamilies": spec.get("ipFamilies", []),
        "ipFamilyPolicy": spec.get("ipFamilyPolicy"),
        "ports": spec.get("ports", []),
        "selector": spec.get("selector", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--kubectl", required=True)
    args = parser.parse_args()
    input_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()
    temp_dir = output_dir.parent / f".{output_dir.name}-building"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    try:
        current, backends, policy = validate_inputs(input_dir)
        temp_dir.mkdir(parents=True)
        bundle = temp_dir / "bundle"
        shutil.copytree(ROOT / "implementation/bundle", bundle)
        for family, file_name in (("IPv4", "slice-ipv4.yaml"), ("IPv6", "slice-ipv6.yaml")):
            slice_path = bundle / file_name
            slice_document = yaml.safe_load(slice_path.read_text(encoding="utf-8"))
            family_rows = sorted(
                [row for row in backends if row["address_family"] == family],
                key=lambda row: row["backend_id"],
            )
            slice_document["ports"] = [{
                "name": policy["service_port_name"],
                "protocol": policy["protocol"],
                "port": policy["service_port"],
            }]
            slice_document["endpoints"] = [
                {
                    "addresses": [row["address"]],
                    "conditions": {"ready": row["ready"] == "true"},
                    "zone": row["zone"],
                }
                for row in family_rows
            ]
            slice_path.write_text(
                yaml.safe_dump(slice_document, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
        rendered_dir = temp_dir / "rendered"
        rendered_dir.mkdir()
        command = subprocess.run([args.kubectl, "kustomize", str(bundle)], text=True, capture_output=True, timeout=120)
        if command.returncode:
            raise ValueError(command.stdout + command.stderr)
        rendered_path = rendered_dir / "payment-discovery.yaml"
        rendered_path.write_text(command.stdout.replace("\r\n", "\n"), encoding="utf-8")
        rendered = documents(rendered_path)
        keys = [
            (item.get("kind", ""), item.get("metadata", {}).get("namespace", ""), item.get("metadata", {}).get("name", ""))
            for item in rendered
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("rendered object key duplicated")
        source_legacy = find(current, "Service", policy["legacy_service"])
        rendered_legacy = find(rendered, "Service", policy["legacy_service"])
        if service_signature(source_legacy) != service_signature(rendered_legacy):
            raise ValueError("legacy service changed")
        dual = find(rendered, "Service", policy["new_service"])
        dual_spec = dual.get("spec", {})
        if dual_spec.get("clusterIP") != "None" or dual_spec.get("clusterIPs") != ["None"] or "selector" in dual_spec:
            raise ValueError("dual service is not headless without selector")
        if dual_spec.get("ipFamilies") != policy["ip_families"] or dual_spec.get("ipFamilyPolicy") != policy["ip_family_policy"]:
            raise ValueError("dual service family policy differs")
        results = temp_dir / "results"
        results.mkdir()
        inventory = [{"kind": kind, "namespace": namespace, "name": name} for kind, namespace, name in sorted(keys)]
        write_csv(results / "object-inventory.csv", ["kind", "namespace", "name"], inventory)
        endpoint_rows = []
        for item in rendered:
            if item.get("kind") != "EndpointSlice":
                continue
            metadata = item.get("metadata", {})
            family = item.get("addressType", "")
            label = metadata.get("labels", {}).get(policy["slice_label"], "")
            ports = item.get("ports", [])
            if len(ports) != 1:
                raise ValueError("slice port differs")
            port = ports[0]
            for endpoint in item.get("endpoints", []):
                for address in endpoint.get("addresses", []):
                    endpoint_rows.append({
                        "slice_name": metadata.get("name", ""),
                        "address_family": family,
                        "address": address,
                        "service_label": label,
                        "port_name": port.get("name", ""),
                        "port": port.get("port", ""),
                        "protocol": port.get("protocol", ""),
                        "ready": str(endpoint.get("conditions", {}).get("ready", "")).lower(),
                        "zone": endpoint.get("zone", ""),
                    })
        endpoint_rows.sort(key=lambda row: (row["address_family"], row["address"]))
        expected_endpoints = sorted(
            [
                {
                    "address_family": row["address_family"], "address": row["address"], "port": row["port"],
                    "ready": row["ready"], "zone": row["zone"],
                }
                for row in backends
            ],
            key=lambda row: (row["address_family"], row["address"]),
        )
        observed_endpoints = [
            {key: str(row[key]) for key in ["address_family", "address", "port", "ready", "zone"]}
            for row in endpoint_rows
        ]
        if observed_endpoints != expected_endpoints:
            raise ValueError("rendered endpoints differ from source")
        if any(row["service_label"] != policy["new_service"] for row in endpoint_rows):
            raise ValueError("EndpointSlice service label differs")
        write_csv(
            results / "endpoint-inventory.csv",
            ["slice_name", "address_family", "address", "service_label", "port_name", "port", "protocol", "ready", "zone"],
            endpoint_rows,
        )
        comparison_fields = ["name", "namespace", "clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy", "ports", "selector"]
        source_sig = service_signature(source_legacy)
        rendered_sig = service_signature(rendered_legacy)
        comparisons = [
            {
                "field": field,
                "source_value": json.dumps(source_sig[field], ensure_ascii=False, sort_keys=True),
                "rendered_value": json.dumps(rendered_sig[field], ensure_ascii=False, sort_keys=True),
                "matches": str(source_sig[field] == rendered_sig[field]).lower(),
            }
            for field in comparison_fields
        ]
        write_csv(results / "legacy-service-diff.csv", ["field", "source_value", "rendered_value", "matches"], comparisons)
        apply_rows = [
            {"sequence": index, "stage": stage, "source": "publish-policy.json"}
            for index, stage in enumerate(policy["apply_order"], 1)
        ]
        write_csv(results / "apply-sequence.csv", ["sequence", "stage", "source"], apply_rows)
        static_review = {
            "status": "READY",
            "review_scope": "LOCAL_RENDERED_MANIFEST_ONLY",
            "legacy_service_unchanged": all(row["matches"] == "true" for row in comparisons),
            "new_service_headless": True,
            "selector_absent": True,
            "families_separated": all(ipaddress.ip_address(row["address"]).version == (4 if row["address_family"] == "IPv4" else 6) for row in endpoint_rows),
            "backend_source_rows": len(backends),
            "source_backends": backends,
            "note": "本地清单不能证明ClusterIP分配、控制器行为、解析结果或网络连通性",
        }
        (results / "static-review.json").write_text(json.dumps(static_review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        release_summary = {
            "status": "READY",
            "review_scope": "LOCAL_RENDERED_MANIFEST_ONLY",
            "post_apply_owner": policy["post_apply_owner"],
            "post_apply_checks": policy["post_apply_checks"],
            "object_count": len(inventory),
            "endpoint_count": len(endpoint_rows),
            "apply_stage_count": len(apply_rows),
        }
        (results / "release-summary.json").write_text(json.dumps(release_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (temp_dir / "tools").mkdir()
        shutil.copy2(Path(__file__), temp_dir / "tools/build_delivery.py")
        (temp_dir / "RELEASE-NOTES.md").write_text(
            "# 支付查询入口双栈发布说明\n\n"
            "当前记录只针对rendered/payment-discovery.yaml中的本地清单，不代表集群已经接受变更。\n\n"
            "现场管理员按results/apply-sequence.csv应用清单后，需要确认Service实际状态、EndpointSlice已接收，并核查双栈解析与连通性。\n",
            encoding="utf-8",
        )
        temp_dir.replace(output_dir)
        return 0
    except Exception as exc:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
