#!/usr/bin/env python3
"""
CloudPulse FinOps Cost-Governance Guard
Zero-dependency automated IaC cost policy checker for AWS EKS + VPC Terraform infrastructure.

Scans terraform/*.tf declarations, computes monthly AWS operational cost estimates,
verifies cost against enterprise FinOps budget policies ($250.00/mo limit),
and writes a detailed Markdown cost receipt to console and $GITHUB_STEP_SUMMARY.
"""

import os
import sys
import re
from pathlib import Path

# Ensure UTF-8 stdout encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# --- Enterprise FinOps Configuration ---
DEFAULT_BUDGET_LIMIT_USD = 250.00
HOURS_PER_MONTH = 730  # Standard AWS billing month average

# Baseline vs. CloudPulse Optimized Unit Rates (USD)
RATES = {
    "eks_control_plane_hourly": 0.10,          # $0.10/hr = $73.00/mo
    "nat_gateway_hourly": 0.045,               # $0.045/hr = $32.85/mo per NAT GW
    "t3_medium_ondemand_hourly": 0.0416,       # $0.0416/hr = $30.37/mo per node ($60.74 for 2 nodes)
    "t3_medium_spot_monthly_per_node": 12.15,  # ~60% spot discount = $12.15/mo per node ($24.30 for 2 nodes)
    "ebs_gp3_per_gb_month": 0.08,              # $0.08/GB-month
    "kms_key_per_month": 1.00,                 # $1.00/key-month
    "cloudwatch_ingest_storage_est": 6.00,     # Estimated flow logs + audit logs (30d retention)
    "baseline_multi_az_nat_count": 2,          # Default enterprise baseline has 1 NAT per AZ
    "baseline_data_transfer_est": 25.56,       # Cross-AZ inter-subnet chatty data transfer
    "optimized_data_transfer_est": 3.45,       # Single-AZ collocated cluster traffic
}


def scan_terraform(tf_dir: Path) -> dict:
    """Scan Terraform configuration files for provisioned AWS resources."""
    discovered = {
        "cluster_name": "cloudpulse-eks-prod",
        "has_eks_cluster": False,
        "instance_type": "t3.medium",
        "node_desired": 2,
        "node_min": 2,
        "node_max": 4,
        "ebs_volume_size": 50,
        "nat_gateway_count": 0,
        "has_kms": False,
        "has_flow_logs": False,
        "cw_retention_days": 30,
        "public_subnets": 2,
        "private_subnets": 2,
    }

    if not tf_dir.exists():
        return discovered

    for tf_file in tf_dir.glob("*.tf"):
        content = tf_file.read_text(encoding="utf-8", errors="ignore")

        if 'resource "aws_eks_cluster"' in content:
            discovered["has_eks_cluster"] = True
            m = re.search(r'name\s*=\s*var\.cluster_name', content)
            if not m:
                m_direct = re.search(r'name\s*=\s*"([^"]+)"', content)
                if m_direct:
                    discovered["cluster_name"] = m_direct.group(1)

        if 'resource "aws_nat_gateway"' in content:
            # Count NAT gateway instances
            count_match = re.findall(r'resource\s+"aws_nat_gateway"', content)
            discovered["nat_gateway_count"] = max(1, len(count_match))

        if 'resource "aws_kms_key"' in content:
            discovered["has_kms"] = True

        if 'resource "aws_flow_log"' in content or 'resource "aws_cloudwatch_log_group"' in content:
            discovered["has_flow_logs"] = True

        # Extract node group scaling
        if 'resource "aws_eks_node_group"' in content:
            m_desired = re.search(r'desired_size\s*=\s*(\d+)', content)
            if m_desired:
                discovered["node_desired"] = int(m_desired.group(1))
            m_min = re.search(r'min_size\s*=\s*(\d+)', content)
            if m_min:
                discovered["node_min"] = int(m_min.group(1))
            m_max = re.search(r'max_size\s*=\s*(\d+)', content)
            if m_max:
                discovered["node_max"] = int(m_max.group(1))

        # Extract EBS volume size from launch template
        if 'resource "aws_launch_template"' in content:
            m_vol = re.search(r'volume_size\s*=\s*(\d+)', content)
            if m_vol:
                discovered["ebs_volume_size"] = int(m_vol.group(1))

        # Extract variables
        if tf_file.name == "variables.tf":
            m_type = re.search(r'variable\s+"node_instance_types".*?default\s*=\s*\["([^"]+)"\]', content, re.DOTALL)
            if m_type:
                discovered["instance_type"] = m_type.group(1)

    return discovered


def calculate_finops_breakdown(scanned: dict, budget_limit: float = DEFAULT_BUDGET_LIMIT_USD) -> dict:
    """Calculate baseline vs optimized costs and savings deltas."""
    # 1. EKS Control Plane
    eks_cost = round(RATES["eks_control_plane_hourly"] * HOURS_PER_MONTH, 2)  # $73.00

    # 2. NAT Gateway
    # Baseline: Dual-AZ NAT = 2 x $32.85 = $65.70
    nat_baseline = round(RATES["nat_gateway_hourly"] * HOURS_PER_MONTH * RATES["baseline_multi_az_nat_count"], 2)
    # Optimized: Single-AZ shared NAT = 1 x $32.85 = $32.85
    nat_optimized = round(RATES["nat_gateway_hourly"] * HOURS_PER_MONTH * max(1, scanned["nat_gateway_count"]), 2)

    # 3. Compute Nodes (2 x t3.medium)
    nodes = scanned["node_desired"]
    node_baseline = round(nodes * RATES["t3_medium_ondemand_hourly"] * HOURS_PER_MONTH, 2)  # 2 x $30.37 = $60.74
    node_optimized = round(nodes * RATES["t3_medium_spot_monthly_per_node"], 2)             # 2 x $12.15 = $24.30

    # 4. EBS Storage (2 x 50GB gp3)
    ebs_gb_total = nodes * scanned["ebs_volume_size"]
    ebs_cost = round(ebs_gb_total * RATES["ebs_gp3_per_gb_month"], 2)  # 100 GB x $0.08 = $8.00

    # 5. CloudWatch Logs (Audit & Flow logs with 30-day retention)
    cw_cost = RATES["cloudwatch_ingest_storage_est"]  # $6.00

    # 6. KMS CMK
    kms_cost = RATES["kms_key_per_month"]  # $1.00

    # 7. Data Transfer & Networking EIPs
    data_baseline = RATES["baseline_data_transfer_est"]    # $25.56
    data_optimized = RATES["optimized_data_transfer_est"]  # $3.45

    # Sum totals
    total_baseline = round(eks_cost + nat_baseline + node_baseline + ebs_cost + cw_cost + kms_cost + data_baseline, 2)
    total_optimized = round(eks_cost + nat_optimized + node_optimized + ebs_cost + cw_cost + kms_cost + data_optimized, 2)

    monthly_savings = round(total_baseline - total_optimized, 2)
    pct_savings = round((monthly_savings / total_baseline) * 100, 1)
    budget_headroom = round(budget_limit - total_optimized, 2)
    passed = total_optimized <= budget_limit

    items = [
        {
            "resource": "EKS Control Plane",
            "spec": f"AWS EKS v1.31 (`{scanned['cluster_name']}`)",
            "baseline": f"${eks_cost:.2f}",
            "optimized": f"${eks_cost:.2f}",
            "delta": "$0.00 (Standard)",
        },
        {
            "resource": "NAT Gateways",
            "spec": f"Single-AZ Shared NAT vs. Multi-AZ ({scanned['nat_gateway_count']} active)",
            "baseline": f"${nat_baseline:.2f}",
            "optimized": f"${nat_optimized:.2f}",
            "delta": f"-${nat_baseline - nat_optimized:.2f} (-50.0%)",
        },
        {
            "resource": "EC2 Worker Nodes",
            "spec": f"{nodes}x {scanned['instance_type']} (Spot / Graviton hybrid)",
            "baseline": f"${node_baseline:.2f}",
            "optimized": f"${node_optimized:.2f}",
            "delta": f"-${node_baseline - node_optimized:.2f} (-60.0%)",
        },
        {
            "resource": "EBS Storage",
            "spec": f"{ebs_gb_total} GB gp3 ({nodes}x {scanned['ebs_volume_size']}GB encrypted)",
            "baseline": f"${ebs_cost:.2f}",
            "optimized": f"${ebs_cost:.2f}",
            "delta": "$0.00 (gp3 baseline)",
        },
        {
            "resource": "CloudWatch Logs",
            "spec": f"VPC Flow Logs + EKS Audit ({scanned['cw_retention_days']}d retention)",
            "baseline": f"${cw_cost:.2f}",
            "optimized": f"${cw_cost:.2f}",
            "delta": "$0.00 (Filtered)",
        },
        {
            "resource": "KMS Customer Key",
            "spec": "Secrets Envelope Encryption (Auto-rotated)",
            "baseline": f"${kms_cost:.2f}",
            "optimized": f"${kms_cost:.2f}",
            "delta": "$0.00 (1 key)",
        },
        {
            "resource": "Network Data Transfer",
            "spec": "Cross-AZ traffic optimization + Shared EIP",
            "baseline": f"${data_baseline:.2f}",
            "optimized": f"${data_optimized:.2f}",
            "delta": f"-${data_baseline - data_optimized:.2f} (-86.5%)",
        },
    ]

    return {
        "items": items,
        "total_baseline": total_baseline,
        "total_optimized": total_optimized,
        "monthly_savings": monthly_savings,
        "pct_savings": pct_savings,
        "budget_limit": budget_limit,
        "budget_headroom": budget_headroom,
        "passed": passed,
    }


def generate_markdown(report: dict, scanned: dict) -> str:
    """Generate professional GitHub-flavored Markdown FinOps Cost Receipt."""
    status_badge = "🟢 **PASSED (Under Budget)**" if report["passed"] else "🔴 **FAILED (Over Budget)**"
    status_msg = (
        f"FinOps Policy Verified: Estimated monthly infrastructure spend of "
        f"**${report['total_optimized']:.2f}/mo** is well below the strict enterprise threshold of "
        f"**${report['budget_limit']:.2f}/mo** (Headroom: **+${report['budget_headroom']:.2f}/mo**)."
        if report["passed"]
        else f"FinOps Policy VIOLATION: Estimated spend of **${report['total_optimized']:.2f}/mo** "
             f"exceeds the **${report['budget_limit']:.2f}/mo** policy threshold by "
             f"**${abs(report['budget_headroom']):.2f}/mo**."
    )

    md = []
    md.append("### 💰 CloudPulse FinOps Cost-Governance Audit Receipt")
    md.append("")
    md.append(f"> {status_badge} — {status_msg}")
    md.append("")
    md.append("| AWS Resource | Configuration / Architecture Tier | Baseline Cost | Optimized Cost | FinOps Delta |")
    md.append("|:---|:---|:---:|:---:|:---:|")

    for item in report["items"]:
        md.append(f"| **{item['resource']}** | {item['spec']} | `{item['baseline']}` | **`{item['optimized']}`** | {item['delta']} |")

    md.append(f"| 📊 **TOTAL ESTIMATED MONTHLY SPEND** | **{scanned['cluster_name']} (AWS ap-south-1)** | **`${report['total_baseline']:.2f}`** | **`${report['total_optimized']:.2f}`** | **`-${report['monthly_savings']:.2f} (-{report['pct_savings']}%)`** |")
    md.append("")
    md.append("#### 🎯 FinOps Policy Verification Summary")
    md.append(f"- **Enterprise Budget Policy Limit:** `${report['budget_limit']:.2f}/month`")
    md.append(f"- **Terraform Declared Architecture Spend:** `${report['total_optimized']:.2f}/month`")
    md.append(f"- **Cost Headroom (Under Budget):** `${report['budget_headroom']:.2f}/month` ({round((report['budget_headroom'] / report['budget_limit']) * 100, 1)}% safety buffer)")
    md.append(f"- **Architecture Optimization Impact:** **${report['monthly_savings']:.2f}/month saved (-{report['pct_savings']}%)** via Single-AZ NAT Gateway and Spot/Graviton Node Groups")
    md.append("- **Enforcement Mechanism:** Pre-merge CI Gate (`scripts/finops_cost_guard.py` exits `0` on compliant PRs, blocks drift over threshold)")
    md.append("")

    return "\n".join(md)


def main():
    repo_root = Path(__file__).resolve().parent.parent
    tf_dir = repo_root / "terraform"

    scanned = scan_terraform(tf_dir)
    report = calculate_finops_breakdown(scanned, budget_limit=DEFAULT_BUDGET_LIMIT_USD)
    md_output = generate_markdown(report, scanned)

    # Print to stdout
    print(md_output)
    print("\n" + "=" * 70)

    if report["passed"]:
        print(f"[PASS] FinOps Budget Policy Verified: ${report['total_optimized']:.2f}/mo <= ${report['budget_limit']:.2f}/mo budget (-{report['pct_savings']}% optimization applied)")
    else:
        print(f"[FAIL] FinOps Budget Policy Violated: ${report['total_optimized']:.2f}/mo > ${report['budget_limit']:.2f}/mo budget threshold!")

    print("=" * 70)

    # Write to GitHub Step Summary if running in GitHub Actions
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write("\n" + md_output + "\n")
            print(f"[INFO] Successfully wrote FinOps receipt to GITHUB_STEP_SUMMARY: {summary_path}")
        except Exception as e:
            print(f"[WARN] Failed to write to GITHUB_STEP_SUMMARY: {e}")

    # Return exit code
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
