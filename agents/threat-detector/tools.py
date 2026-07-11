"""
Read-only AWS security-posture exploration tools for the ThreatDetector agent.

The LLM reasons over these signals like a senior cloud security specialist.
Every tool is Level 1 (read-only). Each is defensive: a service that is not
enabled / not permitted returns {"error": ...} instead of crashing the sweep.
"""
import os
from datetime import datetime, timezone
import hashlib

import boto3

try:
    from strands import tool
except ImportError:  # local/test import without strands
    def tool(f):
        return f

_clients: dict = {}


def _get_credentials() -> dict | None:
    """Target-account credentials via the canonical SDK resolver (ephemeral
    client-side creds → assume-role → None). See sdk.cloud_target."""
    from headlabs_sdk.sdk import resolve_cloud_credentials
    return resolve_cloud_credentials()


def _c(svc: str):
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    creds = _get_credentials()
    key = (svc, region, bool(creds))
    if key not in _clients:
        kwargs = {"region_name": region}
        if creds:
            kwargs.update(creds)
        _clients[key] = boto3.client(svc, **kwargs)
    return _clients[key]


def reset_clients():
    from headlabs_sdk.sdk import reset_cloud_credentials
    _clients.clear()
    reset_cloud_credentials()


# ── LGPD pseudonymization at source (usernames) with auditable mapping ────────
# Stable pseudonym (deterministic) so the same identity correlates across reports.
# The pseudonym -> real value mapping is written to headlabs-pii-map (restricted,
# platform account) so authorized audits/inspections can re-identify later.
_acct = None
_pii_map = None


def _account_id() -> str:
    global _acct
    if _acct is None:
        try:
            _acct = _c("sts").get_caller_identity()["Account"]
        except Exception:
            _acct = "unknown"
    return _acct


def pseudonymize(value: str, kind: str = "iam_user") -> str:
    """Return a stable pseudonym (usr_<hash>) and record the mapping for audit."""
    global _pii_map
    if not value:
        return value
    pid = "usr_" + hashlib.sha256(value.strip().lower().encode()).hexdigest()[:10]
    try:
        if _pii_map is None:
            _pii_map = boto3.resource("dynamodb", region_name="us-east-1").Table(
                os.environ.get("PII_MAP_TABLE", "headlabs-pii-map"))
        _pii_map.put_item(Item={"pseudonym": pid, "value": value, "kind": kind,
                                "account_id": _account_id(),
                                "last_seen": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass  # best-effort; pseudonym is deterministic regardless
    return pid


def _age_days(dt) -> int:
    return (datetime.now(timezone.utc) - dt).days if dt else -1


_SENSITIVE_PORTS = {22, 3389, 3306, 5432, 6379, 27017, 9200, 1433, 5984, 11211}


# ── IAM ─────────────────────────────────────────────────────────────────────

@tool
def iam_users_without_mfa() -> dict:
    """List IAM users that have console access (login profile) but no MFA device."""
    try:
        iam = _c("iam")
        offenders = []
        for u in iam.list_users().get("Users", []):
            name = u["UserName"]
            try:
                iam.get_login_profile(UserName=name)
            except iam.exceptions.NoSuchEntityException:
                continue  # no console access
            if not iam.list_mfa_devices(UserName=name).get("MFADevices"):
                offenders.append(pseudonymize(name))
        return {"users_without_mfa": offenders, "count": len(offenders)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def iam_stale_access_keys(max_age_days: int = 90) -> dict:
    """List active IAM access keys older than max_age_days (rotation hygiene)."""
    try:
        iam = _c("iam")
        stale = []
        for u in iam.list_users().get("Users", []):
            for k in iam.list_access_keys(UserName=u["UserName"]).get("AccessKeyMetadata", []):
                if k["Status"] == "Active":
                    age = _age_days(k["CreateDate"])
                    if age > max_age_days:
                        stale.append({"user": pseudonymize(u["UserName"]), "key_id": k["AccessKeyId"], "age_days": age})
        return {"stale_keys": stale[:50], "count": len(stale)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def iam_admin_entities() -> dict:
    """Find principals/policies granting AdministratorAccess or wildcard *:* permissions."""
    try:
        iam = _c("iam")
        admin = []
        for p in iam.list_policies(Scope="Local", OnlyAttached=True).get("Policies", [])[:200]:
            ver = iam.get_policy_version(PolicyArn=p["Arn"], VersionId=p["DefaultVersionId"])
            stmts = ver["PolicyVersion"]["Document"].get("Statement", [])
            stmts = stmts if isinstance(stmts, list) else [stmts]
            for s in stmts:
                if s.get("Effect") == "Allow" and "*" in (s.get("Action") or "") and "*" in str(s.get("Resource", "")):
                    admin.append({"policy": p["PolicyName"], "attachments": p["AttachmentCount"]})
                    break
        aws_admin = iam.list_entities_for_policy(PolicyArn="arn:aws:iam::aws:policy/AdministratorAccess")
        return {"wildcard_policies": admin,
                "administrator_access": {"users": [pseudonymize(u["UserName"]) for u in aws_admin.get("PolicyUsers", [])],
                                         "roles": [r["RoleName"] for r in aws_admin.get("PolicyRoles", [])]}}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def iam_account_posture() -> dict:
    """Root account usage, account-summary security counters and password policy."""
    try:
        iam = _c("iam")
        summary = iam.get_account_summary().get("SummaryMap", {})
        try:
            pwd = iam.get_account_password_policy().get("PasswordPolicy", {})
        except iam.exceptions.NoSuchEntityException:
            pwd = {"_missing": True}
        return {
            "root_mfa_enabled": summary.get("AccountMFAEnabled") == 1,
            "root_access_keys": summary.get("AccountAccessKeysPresent", 0),
            "mfa_devices": summary.get("MFADevices", 0),
            "users": summary.get("Users", 0),
            "password_policy": pwd,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def access_analyzer_findings() -> dict:
    """IAM Access Analyzer findings (resources shared with external principals)."""
    try:
        aa = _c("accessanalyzer")
        analyzers = aa.list_analyzers(type="ACCOUNT").get("analyzers", [])
        if not analyzers:
            return {"enabled": False, "note": "no account analyzer configured"}
        arn = analyzers[0]["arn"]
        f = aa.list_findings_v2(analyzerArn=arn, filter={"status": {"eq": ["ACTIVE"]}}).get("findings", [])
        return {"enabled": True, "active_findings": len(f),
                "samples": [{"resource": x.get("resource"), "type": x.get("resourceType")} for x in f[:20]]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Network exposure ──────────────────────────────────────────────────────────

@tool
def public_security_groups() -> dict:
    """Security groups allowing 0.0.0.0/0 (or ::/0) ingress on sensitive ports."""
    try:
        ec2 = _c("ec2")
        bad = []
        for sg in ec2.describe_security_groups().get("SecurityGroups", []):
            for perm in sg.get("IpPermissions", []):
                open_v4 = any(r.get("CidrIp") == "0.0.0.0/0" for r in perm.get("IpRanges", []))
                open_v6 = any(r.get("CidrIpv6") == "::/0" for r in perm.get("Ipv6Ranges", []))
                if not (open_v4 or open_v6):
                    continue
                lo, hi = perm.get("FromPort", 0), perm.get("ToPort", 65535)
                hit = [p for p in _SENSITIVE_PORTS if lo <= p <= hi] or (["ALL"] if perm.get("IpProtocol") == "-1" else [])
                if hit:
                    bad.append({"group_id": sg["GroupId"], "name": sg.get("GroupName"), "open_ports": hit})
        return {"exposed_security_groups": bad[:50], "count": len(bad)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def public_rds_instances() -> dict:
    """RDS instances flagged PubliclyAccessible."""
    try:
        rds = _c("rds")
        pub = [{"id": d["DBInstanceIdentifier"], "engine": d.get("Engine"), "encrypted": d.get("StorageEncrypted")}
               for d in rds.describe_db_instances().get("DBInstances", []) if d.get("PubliclyAccessible")]
        return {"public_rds": pub, "count": len(pub)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Data protection (S3) ──────────────────────────────────────────────────────

@tool
def s3_bucket_exposure() -> dict:
    """Buckets missing Public Access Block, default encryption, or with public ACL/policy."""
    try:
        s3 = _c("s3")
        issues = []
        for b in s3.list_buckets().get("Buckets", [])[:100]:
            name = b["Name"]
            row = {"bucket": name, "problems": []}
            try:
                pab = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
                if not all(pab.values()):
                    row["problems"].append("public_access_block_incomplete")
            except Exception:
                row["problems"].append("no_public_access_block")
            try:
                s3.get_bucket_encryption(Bucket=name)
            except Exception:
                row["problems"].append("no_default_encryption")
            try:
                if s3.get_bucket_policy_status(Bucket=name)["PolicyStatus"]["IsPublic"]:
                    row["problems"].append("public_policy")
            except Exception:
                pass
            if row["problems"]:
                issues.append(row)
        return {"buckets_with_issues": issues[:60], "count": len(issues)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Detection services ────────────────────────────────────────────────────────

@tool
def guardduty_findings(severity_min: float = 4.0, max_results: int = 50) -> dict:
    """Active GuardDuty findings with severity >= severity_min (4=medium, 7=high)."""
    try:
        gd = _c("guardduty")
        dets = gd.list_detectors().get("DetectorIds", [])
        if not dets:
            return {"enabled": False, "note": "GuardDuty not enabled"}
        det = dets[0]
        ids = gd.list_findings(DetectorId=det,
                               FindingCriteria={"Criterion": {"severity": {"Gte": severity_min}}},
                               MaxResults=min(max_results, 50)).get("FindingIds", [])
        if not ids:
            return {"enabled": True, "count": 0}
        out = [{"type": f.get("Type"), "severity": f.get("Severity"), "title": f.get("Title"),
                "region": f.get("Region")} for f in gd.get_findings(DetectorId=det, FindingIds=ids).get("Findings", [])]
        return {"enabled": True, "count": len(out), "findings": out[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def macie_findings(max_results: int = 50) -> dict:
    """Macie2 sensitive-data / policy findings."""
    try:
        m = _c("macie2")
        ids = m.list_findings(maxResults=min(max_results, 50)).get("findingIds", [])
        if not ids:
            return {"enabled": True, "count": 0}
        out = [{"type": f.get("type"), "title": f.get("title"),
                "severity": f.get("severity", {}).get("description")}
               for f in m.get_findings(findingIds=ids).get("findings", [])]
        return {"enabled": True, "count": len(out), "findings": out[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def securityhub_findings(max_results: int = 50) -> dict:
    """Security Hub aggregated active findings (CRITICAL/HIGH)."""
    try:
        sh = _c("securityhub")
        resp = sh.get_findings(Filters={
            "SeverityLabel": [{"Value": "CRITICAL", "Comparison": "EQUALS"},
                              {"Value": "HIGH", "Comparison": "EQUALS"}],
            "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
        }, MaxResults=min(max_results, 100))
        out = [{"title": f.get("Title"), "severity": f.get("Severity", {}).get("Label"),
                "resource": (f.get("Resources") or [{}])[0].get("Id")} for f in resp.get("Findings", [])]
        return {"enabled": True, "count": len(out), "findings": out[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def inspector_findings() -> dict:
    """Amazon Inspector2 findings summary by severity."""
    try:
        ins = _c("inspector2")
        agg = {}
        for sev in ("CRITICAL", "HIGH", "MEDIUM"):
            r = ins.list_findings(filterCriteria={"severity": [{"comparison": "EQUALS", "value": sev}]},
                                  maxResults=1).get("findings", [])
            agg[sev] = "present" if r else 0
        return {"by_severity_probe": agg}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Logging / audit ─────────────────────────────────────────────────────────

@tool
def cloudtrail_status() -> dict:
    """CloudTrail coverage: multi-region, log-file validation and logging enabled."""
    try:
        ct = _c("cloudtrail")
        trails = ct.describe_trails().get("trailList", [])
        out = []
        for t in trails:
            st = ct.get_trail_status(Name=t["TrailARN"])
            out.append({"name": t["Name"], "multi_region": t.get("IsMultiRegionTrail"),
                        "log_validation": t.get("LogFileValidationEnabled"),
                        "logging": st.get("IsLogging")})
        return {"trails": out, "any_multi_region": any(x["multi_region"] and x["logging"] for x in out)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def config_compliance() -> dict:
    """AWS Config non-compliant rules."""
    try:
        cfg = _c("config")
        rules = cfg.describe_compliance_by_config_rule(
            ComplianceTypes=["NON_COMPLIANT"]).get("ComplianceByConfigRules", [])
        return {"non_compliant_rules": [r["ConfigRuleName"] for r in rules][:50], "count": len(rules)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Encryption / exposure ─────────────────────────────────────────────────────

@tool
def ebs_encryption() -> dict:
    """EBS default-encryption setting and count of unencrypted volumes."""
    try:
        ec2 = _c("ec2")
        default_on = ec2.get_ebs_encryption_by_default().get("EbsEncryptionByDefault")
        vols = ec2.describe_volumes().get("Volumes", [])
        unenc = [v["VolumeId"] for v in vols if not v.get("Encrypted")]
        return {"encryption_by_default": default_on, "unencrypted_volumes": unenc[:50], "count": len(unenc)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def public_amis_snapshots() -> dict:
    """EBS snapshots / AMIs owned by this account that are publicly shared."""
    try:
        ec2 = _c("ec2")
        snaps = ec2.describe_snapshots(OwnerIds=["self"], RestorableByUserIds=["all"]).get("Snapshots", [])
        imgs = [i["ImageId"] for i in ec2.describe_images(Owners=["self"]).get("Images", [])
                if i.get("Public")]
        return {"public_snapshots": [s["SnapshotId"] for s in snaps][:50], "public_amis": imgs[:50],
                "count": len(snaps) + len(imgs)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def kms_key_rotation() -> dict:
    """Customer-managed KMS keys without automatic rotation enabled."""
    try:
        kms = _c("kms")
        no_rot = []
        for k in kms.list_keys().get("Keys", [])[:200]:
            try:
                meta = kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
                if meta.get("KeyManager") != "CUSTOMER" or meta.get("KeyState") != "Enabled":
                    continue
                if not kms.get_key_rotation_status(KeyId=k["KeyId"]).get("KeyRotationEnabled"):
                    no_rot.append(k["KeyId"])
            except Exception:
                continue
        return {"keys_without_rotation": no_rot[:50], "count": len(no_rot)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Compliance frameworks (Security Hub standards: CIS / NIST / PCI / FSBP) ────

_STD_NAMES = {
    "cis-aws-foundations-benchmark": "CIS AWS Foundations Benchmark",
    "nist-800-53": "NIST SP 800-53 Rev 5",
    "pci-dss": "PCI DSS",
    "aws-foundational-security-best-practices": "AWS Foundational Security Best Practices",
}


@tool
def security_standards_posture() -> dict:
    """Enabled Security Hub compliance standards (CIS, NIST 800-53, PCI DSS, FSBP) and a
    severity breakdown of currently failed controls."""
    try:
        sh = _c("securityhub")
        standards = []
        for s in sh.get_enabled_standards().get("StandardsSubscriptions", []):
            arn = s.get("StandardsArn", "")
            standards.append(next((v for k, v in _STD_NAMES.items() if k in arn), arn.split("/")[-2] if "/" in arn else arn))
        sev: dict = {}
        f = sh.get_findings(Filters={
            "ComplianceStatus": [{"Value": "FAILED", "Comparison": "EQUALS"}],
            "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
        }, MaxResults=100).get("Findings", [])
        for x in f:
            lbl = x.get("Severity", {}).get("Label", "UNKNOWN")
            sev[lbl] = sev.get(lbl, 0) + 1
        return {"enabled_standards": standards or "none (Security Hub standards not enabled)",
                "failed_controls_by_severity": sev, "sampled": len(f)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def failed_security_controls(max_results: int = 30) -> dict:
    """Top failed Security Hub controls (CIS/NIST/PCI/FSBP) — control id, title, severity, resource."""
    try:
        sh = _c("securityhub")
        f = sh.get_findings(Filters={
            "ComplianceStatus": [{"Value": "FAILED", "Comparison": "EQUALS"}],
            "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
            "SeverityLabel": [{"Value": v, "Comparison": "EQUALS"} for v in ("CRITICAL", "HIGH")],
        }, MaxResults=min(max_results, 100)).get("Findings", [])
        out = [{"control": x.get("Compliance", {}).get("SecurityControlId") or x.get("GeneratorId"),
                "title": x.get("Title"), "severity": x.get("Severity", {}).get("Label"),
                "resource": (x.get("Resources") or [{}])[0].get("Id")} for x in f]
        return {"failed_controls": out[:max_results], "count": len(out)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Vulnerabilities — Amazon Inspector v2 (EC2 + ECR containers + Lambda) ──────

@tool
def inspector_vulnerabilities() -> dict:
    """Inspector vulnerability (CVE) posture: scan enablement + most vulnerable EC2 instances
    and ECR container images by critical/high counts."""
    try:
        ins = _c("inspector2")
        acct = ins.batch_get_account_status().get("accounts", [{}])
        state = acct[0].get("resourceState", {}) if acct else {}

        def agg(atype, key, idfield):
            r = ins.list_finding_aggregations(aggregationType=atype, maxResults=10).get("responses", [])
            rows = []
            for x in r:
                a = x.get(key, {}) or {}
                sc = a.get("severityCounts", {}) or {}
                rows.append({"resource": a.get(idfield), "critical": sc.get("critical", 0), "high": sc.get("high", 0)})
            return [o for o in rows if o["critical"] or o["high"]][:10]

        return {
            "scan_status": {k: (state.get(k, {}) or {}).get("status") for k in ("ec2", "ecr", "lambda")},
            "vulnerable_ec2": agg("AWS_EC2_INSTANCE", "ec2InstanceAggregation", "instanceId"),
            "vulnerable_container_images": agg("AWS_ECR_CONTAINER_IMAGE", "awsEcrContainerAggregation", "repository"),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@tool
def inspector_top_cves() -> dict:
    """Top CVEs across the account (Inspector), with severity counts — prioritize fixable/critical."""
    try:
        ins = _c("inspector2")
        r = ins.list_finding_aggregations(aggregationType="TITLE", maxResults=15).get("responses", [])
        out = []
        for x in r:
            a = x.get("titleAggregation", {}) or {}
            sc = a.get("severityCounts", {}) or {}
            out.append({"cve": a.get("title"), "vulnerability_id": a.get("vulnerabilityId"),
                        "critical": sc.get("critical", 0), "high": sc.get("high", 0)})
        return {"top_cves": [o for o in out if o["critical"] or o["high"]][:15]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Effective internet-reachable ports (config-derived "port scan") ───────────

def _sg_internet_ports():
    """Map security-group-id -> list of ports open to 0.0.0.0/0 or ::/0."""
    ec2 = _c("ec2")
    out: dict = {}
    for sg in ec2.describe_security_groups().get("SecurityGroups", []):
        ports = []
        for p in sg.get("IpPermissions", []):
            if not (any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", []))
                    or any(r.get("CidrIpv6") == "::/0" for r in p.get("Ipv6Ranges", []))):
                continue
            if p.get("IpProtocol") == "-1":
                ports.append("ALL")
            else:
                lo, hi, proto = p.get("FromPort"), p.get("ToPort"), p.get("IpProtocol")
                ports.append(f"{lo}/{proto}" if lo == hi else f"{lo}-{hi}/{proto}")
        if ports:
            out[sg["GroupId"]] = ports
    return out


@tool
def internet_exposed_ports() -> dict:
    """Effective internet-reachable ports (read-only, config-derived — the AWS-native
    equivalent of an external port scan). Cross-references public-IP ENIs (EC2 servers,
    ECS/Fargate tasks, etc.) with their security groups' 0.0.0.0/0 open ports, plus
    internet-facing load balancer listeners."""
    try:
        ec2 = _c("ec2")
        sg_open = _sg_internet_ports()
        exposed = []
        for eni in ec2.describe_network_interfaces().get("NetworkInterfaces", []):
            pub = eni.get("Association", {}).get("PublicIp")
            itype = eni.get("InterfaceType", "interface")
            if not pub or itype == "nat_gateway":
                continue
            ports = sorted({pp for g in eni.get("Groups", []) for pp in sg_open.get(g["GroupId"], [])})
            if not ports:
                continue
            exposed.append({
                "public_ip": pub,
                "type": itype,  # interface (EC2/ECS task) | network_load_balancer | lambda | ...
                "attached_to": eni.get("Attachment", {}).get("InstanceId") or eni.get("Description") or eni.get("RequesterId"),
                "open_ports": ports,
            })
        lbs = []
        try:
            elb = _c("elbv2")
            for lb in elb.describe_load_balancers().get("LoadBalancers", []):
                if lb.get("Scheme") != "internet-facing":
                    continue
                ls = elb.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"]).get("Listeners", [])
                lbs.append({"name": lb["LoadBalancerName"], "type": lb.get("Type"), "dns": lb.get("DNSName"),
                            "listener_ports": [f'{x.get("Port")}/{x.get("Protocol")}' for x in ls]})
        except Exception:
            pass
        return {"internet_exposed_resources": exposed[:60], "count": len(exposed),
                "internet_facing_load_balancers": lbs[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Secret-name heuristic (shared) ────────────────────────────────────────────
_SECRET_HINTS = ("password", "passwd", "pwd", "secret", "token", "apikey", "api_key",
                 "access_key", "accesskey", "private_key", "privatekey", "credential",
                 "cred", "client_secret", "auth")


def _looks_secret(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _SECRET_HINTS)


# ── Instance Metadata Service (IMDSv1 / SSRF → credential theft) ───────────────

@tool
def ec2_imdsv1_exposure() -> dict:
    """EC2 instances that still allow IMDSv1 (MetadataOptions.HttpTokens != 'required').

    IMDSv1 is the classic SSRF → instance-role-credential-theft path: an SSRF in
    an app can read http://169.254.169.254/ and steal the role's creds. Also flags
    a metadata hop limit > 1 (lets containers reach the host IMDS)."""
    try:
        ec2 = _c("ec2")
        offenders = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(Filters=[{"Name": "instance-state-name",
                                                  "Values": ["running", "stopped"]}]):
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    opts = inst.get("MetadataOptions", {}) or {}
                    if opts.get("HttpEndpoint") == "disabled":
                        continue
                    imdsv1_ok = opts.get("HttpTokens") != "required"
                    hop = opts.get("HttpPutResponseHopLimit", 1)
                    if imdsv1_ok or (isinstance(hop, int) and hop > 1):
                        has_role = bool(inst.get("IamInstanceProfile"))
                        offenders.append({
                            "instance_id": inst["InstanceId"],
                            "imdsv1_allowed": imdsv1_ok,
                            "hop_limit": hop,
                            "has_instance_role": has_role,  # role => stealable creds
                        })
        return {"imdsv1_instances": offenders[:60], "count": len(offenders)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── IAM role trust policy / cross-account exposure ────────────────────────────

@tool
def iam_role_trust_exposure() -> dict:
    """IAM roles whose trust policy (AssumeRolePolicyDocument) is dangerously broad:
    Principal '*' (anyone can assume), or an EXTERNAL account allowed WITHOUT a
    Condition (no ExternalId / no aws:SourceArn) — the classic confused-deputy hole."""
    try:
        iam = _c("iam")
        this_acct = _account_id()
        risky = []
        paginator = iam.get_paginator("list_roles")
        for page in paginator.paginate():
            for r in page.get("Roles", []):
                doc = r.get("AssumeRolePolicyDocument") or {}
                stmts = doc.get("Statement", [])
                stmts = stmts if isinstance(stmts, list) else [stmts]
                for s in stmts:
                    if s.get("Effect") != "Allow":
                        continue
                    principal = s.get("Principal", {})
                    has_cond = bool(s.get("Condition"))
                    aws_p = principal.get("AWS") if isinstance(principal, dict) else principal
                    vals = aws_p if isinstance(aws_p, list) else [aws_p]
                    for v in vals:
                        v = str(v or "")
                        if v == "*" or principal == "*":
                            risky.append({"role": r["RoleName"], "issue": "principal_wildcard",
                                          "has_condition": has_cond})
                        elif v.startswith("arn:aws:iam::"):
                            acct = v.split(":")[4] if len(v.split(":")) > 4 else ""
                            if acct and acct != this_acct and not has_cond:
                                risky.append({"role": r["RoleName"], "issue": "external_account_no_condition",
                                              "external_account": acct, "has_condition": False})
        return {"risky_trust_roles": risky[:60], "count": len(risky)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Secrets exposure (Lambda env, SSM params, Secrets Manager rotation) ───────

@tool
def secrets_exposure() -> dict:
    """Plaintext/unrotated secrets across the account:
    - Lambda env vars with secret-looking keys (creds in plaintext config),
    - SSM Parameter Store params of type String (not SecureString) with secret-looking names,
    - Secrets Manager secrets with rotation disabled."""
    out = {"lambda_env_secrets": [], "ssm_plaintext_secrets": [], "secrets_without_rotation": []}
    try:
        lam = _c("lambda")
        for page in lam.get_paginator("list_functions").paginate():
            for fn in page.get("Functions", []):
                env = (fn.get("Environment") or {}).get("Variables", {}) or {}
                hits = [k for k in env if _looks_secret(k)]
                if hits:
                    out["lambda_env_secrets"].append({"function": fn["FunctionName"], "keys": hits[:10]})
    except Exception as e:  # noqa: BLE001
        out["lambda_error"] = str(e)
    try:
        ssm = _c("ssm")
        for page in ssm.get_paginator("describe_parameters").paginate():
            for p in page.get("Parameters", []):
                if p.get("Type") == "String" and _looks_secret(p.get("Name", "")):
                    out["ssm_plaintext_secrets"].append({"name": p["Name"], "type": p["Type"]})
    except Exception as e:  # noqa: BLE001
        out["ssm_error"] = str(e)
    try:
        sm = _c("secretsmanager")
        for page in sm.get_paginator("list_secrets").paginate():
            for s in page.get("SecretList", []):
                if not s.get("RotationEnabled"):
                    out["secrets_without_rotation"].append(s.get("Name"))
    except Exception as e:  # noqa: BLE001
        out["secretsmanager_error"] = str(e)
    out["lambda_env_secrets"] = out["lambda_env_secrets"][:40]
    out["ssm_plaintext_secrets"] = out["ssm_plaintext_secrets"][:40]
    out["secrets_without_rotation"] = out["secrets_without_rotation"][:40]
    out["count"] = (len(out["lambda_env_secrets"]) + len(out["ssm_plaintext_secrets"])
                    + len(out["secrets_without_rotation"]))
    return out


# ── Public managed data stores (beyond S3/RDS instances) ──────────────────────

@tool
def public_managed_datastores() -> dict:
    """Managed data stores exposed beyond their account:
    - RDS/Aurora manual snapshots shared publicly ('all'),
    - Redshift clusters PubliclyAccessible,
    - OpenSearch/Elasticsearch domains with an access policy allowing Principal '*'."""
    out = {"public_rds_snapshots": [], "public_aurora_snapshots": [],
           "public_redshift_clusters": [], "open_opensearch_domains": []}
    try:
        rds = _c("rds")
        for s in rds.describe_db_snapshots(SnapshotType="manual").get("DBSnapshots", [])[:100]:
            attrs = rds.describe_db_snapshot_attributes(
                DBSnapshotIdentifier=s["DBSnapshotIdentifier"]
            ).get("DBSnapshotAttributesResult", {}).get("DBSnapshotAttributes", [])
            if any("all" in (a.get("AttributeValues") or []) for a in attrs if a.get("AttributeName") == "restore"):
                out["public_rds_snapshots"].append(s["DBSnapshotIdentifier"])
        for s in rds.describe_db_cluster_snapshots(SnapshotType="manual").get("DBClusterSnapshots", [])[:100]:
            attrs = rds.describe_db_cluster_snapshot_attributes(
                DBClusterSnapshotIdentifier=s["DBClusterSnapshotIdentifier"]
            ).get("DBClusterSnapshotAttributesResult", {}).get("DBClusterSnapshotAttributes", [])
            if any("all" in (a.get("AttributeValues") or []) for a in attrs if a.get("AttributeName") == "restore"):
                out["public_aurora_snapshots"].append(s["DBClusterSnapshotIdentifier"])
    except Exception as e:  # noqa: BLE001
        out["rds_error"] = str(e)
    try:
        rs = _c("redshift")
        for c in rs.describe_clusters().get("Clusters", []):
            if c.get("PubliclyAccessible"):
                out["public_redshift_clusters"].append(c["ClusterIdentifier"])
    except Exception as e:  # noqa: BLE001
        out["redshift_error"] = str(e)
    try:
        es = _c("opensearch")
        for d in es.list_domain_names().get("DomainNames", []):
            name = d.get("DomainName")
            try:
                cfg = es.describe_domain(DomainName=name).get("DomainStatus", {})
                pol = cfg.get("AccessPolicies") or ""
                if '"Principal":"*"' in pol.replace(" ", "") or '"AWS":"*"' in pol.replace(" ", ""):
                    out["open_opensearch_domains"].append(name)
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        out["opensearch_error"] = str(e)
    out["count"] = sum(len(out[k]) for k in
                       ("public_rds_snapshots", "public_aurora_snapshots",
                        "public_redshift_clusters", "open_opensearch_domains"))
    return out


# ── Lambda public access (function URLs / resource policy) ────────────────────

@tool
def lambda_public_access() -> dict:
    """Lambda functions reachable by anyone:
    - Function URLs with AuthType 'NONE' (public HTTPS endpoint, no IAM auth),
    - Resource policies granting invoke to Principal '*' without a source condition."""
    out = {"public_function_urls": [], "public_resource_policies": []}
    try:
        lam = _c("lambda")
        import json as _json
        for page in lam.get_paginator("list_functions").paginate():
            for fn in page.get("Functions", []):
                name = fn["FunctionName"]
                try:
                    cfg = lam.get_function_url_config(FunctionName=name)
                    if cfg.get("AuthType") == "NONE":
                        out["public_function_urls"].append({"function": name, "url": cfg.get("FunctionUrl")})
                except Exception:
                    pass
                try:
                    pol = _json.loads(lam.get_policy(FunctionName=name).get("Policy", "{}"))
                    for s in (pol.get("Statement") or []):
                        pr = s.get("Principal", {})
                        pr_val = pr.get("AWS") if isinstance(pr, dict) else pr
                        if (pr == "*" or pr_val == "*") and not s.get("Condition"):
                            out["public_resource_policies"].append(name)
                            break
                except Exception:
                    pass
        out["public_function_urls"] = out["public_function_urls"][:40]
        out["public_resource_policies"] = out["public_resource_policies"][:40]
        out["count"] = len(out["public_function_urls"]) + len(out["public_resource_policies"])
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── Monitoring coverage (VPC Flow Logs + CIS CloudWatch alarms) ───────────────

_CIS_MONITORING_HINTS = ("root", "unauthorized", "ConsoleSignin", "iam", "policy",
                         "SecurityGroup", "NetworkAcl", "Gateway", "RouteTable",
                         "cloudtrail", "kms", "s3")


@tool
def monitoring_coverage() -> dict:
    """Detective-control coverage gaps:
    - VPCs without VPC Flow Logs (no network forensics on that VPC),
    - whether CIS-style CloudWatch metric filters/alarms exist (root usage, unauthorized
      API calls, IAM/SG/NACL/route changes) — their absence means key events go unalerted."""
    out = {}
    try:
        ec2 = _c("ec2")
        vpcs = [v["VpcId"] for v in ec2.describe_vpcs().get("Vpcs", [])]
        logged = set()
        for fl in ec2.describe_flow_logs().get("FlowLogs", []):
            if fl.get("ResourceId"):
                logged.add(fl["ResourceId"])
        out["vpcs_without_flow_logs"] = [v for v in vpcs if v not in logged][:50]
        out["vpc_count"] = len(vpcs)
    except Exception as e:  # noqa: BLE001
        out["flow_logs_error"] = str(e)
    try:
        logs = _c("logs")
        matched = 0
        total = 0
        for page in logs.get_paginator("describe_metric_filters").paginate():
            for mf in page.get("metricFilters", []):
                total += 1
                pat = (mf.get("filterPattern") or "")
                if any(h.lower() in pat.lower() for h in _CIS_MONITORING_HINTS):
                    matched += 1
        out["cis_metric_filters_present"] = matched
        out["total_metric_filters"] = total
    except Exception as e:  # noqa: BLE001
        out["metric_filters_error"] = str(e)
    try:
        cw = _c("cloudwatch")
        out["cloudwatch_alarms"] = len(cw.describe_alarms(MaxRecords=100).get("MetricAlarms", []))
    except Exception as e:  # noqa: BLE001
        out["alarms_error"] = str(e)
    return out


# ── ECR image hygiene + base-image / CVE posture ──────────────────────────────

@tool
def ecr_image_hygiene(stale_days: int = 180) -> dict:
    """Container supply-chain hygiene for ECR:
    - repos with scan-on-push OFF or MUTABLE tags (no immutability),
    - registry enhanced (Inspector) scanning enabled?,
    - per-repo latest-image CVE counts (critical/high) and base-image signal:
      the image platform/OS + how stale the newest image is (old, never-rebuilt
      images keep vulnerable base layers). NOTE: read-only AWS cannot compare a
      base tag against its upstream registry — staleness + OS + CVE layer is the
      observable proxy for an outdated base image."""
    try:
        ecr = _c("ecr")
        try:
            reg = ecr.get_registry_scanning_configuration().get("scanningConfiguration", {})
            scan_type = reg.get("scanType", "BASIC")
        except Exception:
            scan_type = "unknown"
        repos_out = []
        for page in ecr.get_paginator("describe_repositories").paginate():
            for repo in page.get("repositories", []):
                name = repo["repositoryName"]
                row = {"repository": name,
                       "scan_on_push": (repo.get("imageScanningConfiguration") or {}).get("scanOnPush", False),
                       "tag_mutability": repo.get("imageTagMutability"),
                       "problems": []}
                if not row["scan_on_push"]:
                    row["problems"].append("scan_on_push_off")
                if row["tag_mutability"] == "MUTABLE":
                    row["problems"].append("mutable_tags")
                # Newest image: age (stale base) + platform/OS + CVE counts.
                try:
                    imgs = ecr.describe_images(repositoryName=name,
                                               filter={"tagStatus": "TAGGED"}).get("imageDetails", [])
                    if imgs:
                        newest = max(imgs, key=lambda i: i.get("imagePushedAt") or datetime.min.replace(tzinfo=timezone.utc))
                        age = _age_days(newest.get("imagePushedAt"))
                        row["newest_image_age_days"] = age
                        if age > stale_days:
                            row["problems"].append("stale_image_base_likely_outdated")
                        fsc = newest.get("imageScanFindingsSummary", {}) or {}
                        counts = fsc.get("findingSeverityCounts", {}) or {}
                        row["critical_cves"] = counts.get("CRITICAL", 0)
                        row["high_cves"] = counts.get("HIGH", 0)
                        tag = (newest.get("imageTags") or ["<untagged>"])[0]
                        # Base-image / OS signal from the newest image's scan findings.
                        try:
                            sf = ecr.describe_image_scan_findings(
                                repositoryName=name, imageId={"imageTag": tag}, maxResults=50)
                            os_pkgs = 0
                            for f in (sf.get("imageScanFindings", {}) or {}).get("findings", []):
                                attrs = {a["key"]: a["value"] for a in f.get("attributes", [])}
                                if attrs.get("package_name") or "CVE" in (f.get("name") or ""):
                                    os_pkgs += 1
                            row["base_layer_cve_findings"] = os_pkgs
                        except Exception:
                            pass
                        if row.get("critical_cves") or row.get("high_cves"):
                            row["problems"].append("vulnerable_image_cves")
                except Exception:
                    pass
                if row["problems"]:
                    repos_out.append(row)
        return {"registry_scan_type": scan_type,
                "enhanced_scanning": scan_type == "ENHANCED",
                "repositories_with_issues": repos_out[:50], "count": len(repos_out)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


# ── ACM certificate expiry ────────────────────────────────────────────────────

@tool
def acm_certificate_expiry(warn_days: int = 30) -> dict:
    """ACM certificates that are expired or expiring within warn_days — an expired
    cert on a public endpoint causes outage and erodes TLS trust. In-use certs are
    higher priority."""
    try:
        acm = _c("acm")
        now = datetime.now(timezone.utc)
        out = []
        for page in acm.get_paginator("list_certificates").paginate():
            for c in page.get("CertificateSummaryList", []):
                arn = c["CertificateArn"]
                try:
                    d = acm.describe_certificate(CertificateArn=arn).get("Certificate", {})
                except Exception:
                    continue
                not_after = d.get("NotAfter")
                if not not_after:
                    continue
                days_left = (not_after - now).days
                if days_left <= warn_days:
                    out.append({
                        "domain": d.get("DomainName"),
                        "days_left": days_left,
                        "expired": days_left < 0,
                        "in_use": bool(d.get("InUseBy")),
                        "status": d.get("Status"),
                    })
        out.sort(key=lambda x: x["days_left"])
        return {"expiring_certificates": out[:50], "count": len(out)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
