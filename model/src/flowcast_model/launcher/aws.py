"""EC2 Spot training runs: shared resources, launch, status, kill, cost.

Every resource is tagged `project=flowcast component=training`. A run is one persistent Spot instance (interruption
behavior `stop`, so an interrupted instance keeps its EBS root and resumes when capacity returns) that pulls the
code and dataset, trains with per-epoch checkpoints synced to S3, hindcasts the validation years, uploads results
and terminates itself. Three independent limits stop anything from running past `max_hours`:

1. the Spot request's `ValidUntil` (no relaunch after the deadline);
2. an on-instance watchdog (graceful stop 15 min before, then sync and self-terminate at the deadline);
3. two EventBridge Scheduler one-shot schedules that cancel the Spot request and terminate the instance a few
   minutes after the deadline, even if the instance is wedged.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

TAGS = {"project": "flowcast", "component": "training"}
NAME = "flowcast-training"
INSTANCE_ROLE = "flowcast-training-instance"
REAPER_ROLE = "flowcast-training-reaper"
SCHEDULE_GROUP = "flowcast-training"
AMI_PARAMETER = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
EBS_GB = 100
EBS_USD_PER_GB_MONTH = 0.08
PUBLIC_IPV4_USD_PER_H = 0.005


def tag_list(extra: dict | None = None) -> list[dict]:
    return [{"Key": k, "Value": str(v)} for k, v in {**TAGS, **(extra or {})}.items()]


@dataclass
class Account:
    session: boto3.Session = field(default_factory=boto3.Session)

    def __post_init__(self):
        self.region = self.session.region_name
        self.account_id = self.session.client("sts").get_caller_identity()["Account"]
        self.bucket = f"{NAME}-{self.account_id}"
        self.dataset_bucket = f"flowcast-dataset-{self.account_id}"

    def client(self, name: str):
        return self.session.client(name)


# ---------------------------------------------------------------------- setup


def _ensure_bucket(acct: Account) -> None:
    s3 = acct.client("s3")
    try:
        s3.head_bucket(Bucket=acct.bucket)
    except ClientError:
        try:
            s3.create_bucket(Bucket=acct.bucket, CreateBucketConfiguration={"LocationConstraint": acct.region})
        except ClientError as err:
            # the S3 default region rejects an explicit location constraint
            if err.response["Error"]["Code"] != "InvalidLocationConstraint":
                raise
            s3.create_bucket(Bucket=acct.bucket)
        log.info("created bucket %s", acct.bucket)
    s3.put_public_access_block(Bucket=acct.bucket, PublicAccessBlockConfiguration={k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    s3.put_bucket_tagging(Bucket=acct.bucket, Tagging={"TagSet": tag_list()})
    s3.put_bucket_lifecycle_configuration(
        Bucket=acct.bucket,
        LifecycleConfiguration={
            "Rules": [
                {"ID": "abort-multipart", "Status": "Enabled", "Filter": {"Prefix": ""}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}},
                {"ID": "expire-code-bundles", "Status": "Enabled", "Filter": {"Prefix": "code/"}, "Expiration": {"Days": 60}},
            ]
        },
    )


def _instance_policy(acct: Account) -> dict:
    buckets = [acct.bucket, acct.dataset_bucket]
    tag_cond = {"StringEquals": {"aws:ResourceTag/project": "flowcast", "aws:ResourceTag/component": "training"}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{b}" for b in buckets]},
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{b}/*" for b in buckets]},
            {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject"], "Resource": [f"arn:aws:s3:::{acct.bucket}/*"]},
            {"Effect": "Allow", "Action": ["ec2:DescribeInstances", "ec2:DescribeSpotInstanceRequests", "ec2:DescribeTags", "ec2:DescribeSpotPriceHistory"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ec2:TerminateInstances", "ec2:CancelSpotInstanceRequests"], "Resource": "*", "Condition": tag_cond},
        ],
    }


def _reaper_policy() -> dict:
    tag_cond = {"StringEquals": {"aws:ResourceTag/project": "flowcast", "aws:ResourceTag/component": "training"}}
    return {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["ec2:TerminateInstances", "ec2:CancelSpotInstanceRequests"], "Resource": "*", "Condition": tag_cond}],
    }


def _ensure_role(iam, name: str, service: str, policy: dict, managed: list[str] = ()) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=tag_list(), Description="flowcast training (managed by flowcast-train setup)")["Role"]["Arn"]
        log.info("created role %s", name)
    iam.put_role_policy(RoleName=name, PolicyName=name, PolicyDocument=json.dumps(policy))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    return arn


def _ensure_instance_profile(iam) -> str:
    try:
        profile = iam.get_instance_profile(InstanceProfileName=INSTANCE_ROLE)["InstanceProfile"]
    except iam.exceptions.NoSuchEntityException:
        profile = iam.create_instance_profile(InstanceProfileName=INSTANCE_ROLE, Tags=tag_list())["InstanceProfile"]
        time.sleep(5)
    if not any(r["RoleName"] == INSTANCE_ROLE for r in profile.get("Roles", [])):
        iam.add_role_to_instance_profile(InstanceProfileName=INSTANCE_ROLE, RoleName=INSTANCE_ROLE)
        time.sleep(10)  # instance profiles take a few seconds to propagate
    return profile["Arn"]


def _ensure_security_group(ec2) -> tuple[str, str]:
    vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    groups = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [NAME]}, {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    if groups:
        return groups[0]["GroupId"], vpc
    sg = ec2.create_security_group(
        GroupName=NAME,
        Description="flowcast training instances: no inbound, all outbound",
        VpcId=vpc,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": tag_list({"Name": NAME})}],
    )["GroupId"]
    log.info("created security group %s", sg)
    return sg, vpc


def _ensure_schedule_group(scheduler) -> None:
    try:
        scheduler.get_schedule_group(Name=SCHEDULE_GROUP)
    except scheduler.exceptions.ResourceNotFoundException:
        scheduler.create_schedule_group(Name=SCHEDULE_GROUP, Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])


def setup(acct: Account) -> dict:
    """Create (or update) the shared training resources. Idempotent."""
    iam = acct.client("iam")
    _ensure_bucket(acct)
    _ensure_role(iam, INSTANCE_ROLE, "ec2.amazonaws.com", _instance_policy(acct), ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"])
    profile = _ensure_instance_profile(iam)
    reaper = _ensure_role(iam, REAPER_ROLE, "scheduler.amazonaws.com", _reaper_policy())
    sg, vpc = _ensure_security_group(acct.client("ec2"))
    _ensure_schedule_group(acct.client("scheduler"))
    return {"bucket": acct.bucket, "instance_profile": profile, "reaper_role": reaper, "security_group": sg, "vpc": vpc}


# ---------------------------------------------------------------------- launch


def package_code(repo: Path) -> tuple[bytes, str]:
    """Tarball of the working tree's tracked files (uncommitted edits included) and its content id."""
    stash = subprocess.run(["git", "stash", "create"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    treeish = stash or "HEAD"
    sha = subprocess.run(["git", "rev-parse", "--short=12", treeish], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(stash)
    data = subprocess.run(["git", "archive", "--format=tar.gz", treeish, "model", "evaluation", "pipeline"], cwd=repo, capture_output=True, check=True).stdout
    return data, f"{sha}{'-dirty' if dirty else ''}"


def _render_user_data(env: dict[str, str]) -> str:
    template = resources.files("flowcast_model.launcher").joinpath("bootstrap.sh").read_text()
    header = "\n".join(f"{k}={json.dumps(str(v))}" for k, v in env.items())
    script = template.replace("#__FLOWCAST_ENV__", header)
    if len(script.encode()) > 16000:
        raise ValueError("user data exceeds the 16 KB EC2 limit")
    return script


def _subnets_by_price(ec2, vpc: str, instance_type: str) -> list[tuple[str, str, float]]:
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
    offered = {
        o["Location"]
        for o in ec2.describe_instance_type_offerings(LocationType="availability-zone", Filters=[{"Name": "instance-type", "Values": [instance_type]}])["InstanceTypeOfferings"]
    }
    prices = {}
    history = ec2.describe_spot_price_history(InstanceTypes=[instance_type], ProductDescriptions=["Linux/UNIX"], StartTime=datetime.now(timezone.utc))["SpotPriceHistory"]
    for h in history:
        prices.setdefault(h["AvailabilityZone"], float(h["SpotPrice"]))
    rows = [(s["SubnetId"], s["AvailabilityZone"], prices.get(s["AvailabilityZone"], 99.0)) for s in subnets if s["AvailabilityZone"] in offered]
    return sorted(rows, key=lambda r: r[2])


def resolve_ami(acct: Account) -> str:
    return acct.client("ssm").get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]


@dataclass
class RunSpec:
    run_id: str
    config: dict
    overrides: dict


def launch(
    acct: Account,
    runs: list[RunSpec],
    dataset_uris: list[str],
    repo: Path,
    instance_type: str = "g5.2xlarge",
    max_hours: float = 3.0,
    max_price: float | None = None,
    sweep: str | None = None,
) -> list[dict]:
    res = setup(acct)
    s3, ec2, scheduler = acct.client("s3"), acct.client("ec2"), acct.client("scheduler")
    code, code_id = package_code(repo)
    code_key = f"code/{code_id}.tar.gz"
    s3.put_object(Bucket=acct.bucket, Key=code_key, Body=code, Tagging="project=flowcast&component=training")
    ami = resolve_ami(acct)
    subnets = _subnets_by_price(ec2, res["vpc"], instance_type)
    launched = []
    for spec in runs:
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=max_hours)
        prefix = f"runs/{spec.run_id}"
        s3.put_object(Bucket=acct.bucket, Key=f"{prefix}/config.yml", Body=yaml.safe_dump(spec.config, sort_keys=False).encode())
        env = {
            "RUN_ID": spec.run_id,
            "BUCKET": acct.bucket,
            "REGION": acct.region,
            "CODE_URI": f"s3://{acct.bucket}/{code_key}",
            "DATASET_URIS": " ".join(dataset_uris),
            "DEADLINE_EPOCH": int(deadline.timestamp()),
            "MAX_BOOTS": 8,
        }
        user_data = _render_user_data(env)
        tags = {"Name": f"flowcast-train-{spec.run_id}"[:255], "flowcast:run": spec.run_id, "flowcast:deadline": deadline.isoformat(timespec="seconds")}
        if sweep:
            tags["flowcast:sweep"] = sweep
        spot = {"SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop", "ValidUntil": deadline}
        if max_price:
            spot["MaxPrice"] = f"{max_price:.4f}"
        instance = None
        errors = []
        for subnet, az, price in subnets:
            try:
                instance = ec2.run_instances(
                    ImageId=ami,
                    InstanceType=instance_type,
                    MinCount=1,
                    MaxCount=1,
                    IamInstanceProfile={"Name": INSTANCE_ROLE},
                    NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet, "Groups": [res["security_group"]], "AssociatePublicIpAddress": True, "DeleteOnTermination": True}],
                    BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": EBS_GB, "VolumeType": "gp3", "DeleteOnTermination": True}}],
                    InstanceMarketOptions={"MarketType": "spot", "SpotOptions": spot},
                    MetadataOptions={"HttpTokens": "required", "InstanceMetadataTags": "enabled", "HttpEndpoint": "enabled"},
                    UserData=user_data,
                    TagSpecifications=[{"ResourceType": r, "Tags": tag_list(tags)} for r in ("instance", "volume", "spot-instances-request", "network-interface")],
                )["Instances"][0]
                log.info("%s: launched %s in %s (spot ~$%.3f/h)", spec.run_id, instance["InstanceId"], az, price)
                break
            except ClientError as err:
                code_ = err.response["Error"]["Code"]
                errors.append(f"{az}: {code_}")
                if code_ not in ("InsufficientInstanceCapacity", "SpotMaxPriceTooLow", "Unsupported", "InsufficientCapacity", "MaxSpotInstanceCountExceeded"):
                    raise
        if instance is None:
            raise RuntimeError(f"no Spot capacity for {instance_type}: {errors}")
        iid = instance["InstanceId"]
        sir = instance.get("SpotInstanceRequestId")
        _schedule_reaper(scheduler, res["reaper_role"], spec.run_id, iid, sir, deadline)
        manifest = {
            "run_id": spec.run_id,
            "sweep": sweep,
            "overrides": spec.overrides,
            "instance_id": iid,
            "spot_request_id": sir,
            "instance_type": instance_type,
            "availability_zone": instance["Placement"]["AvailabilityZone"],
            "ami": ami,
            "code": env["CODE_URI"],
            "datasets": dataset_uris,
            "launched": now.isoformat(timespec="seconds"),
            "deadline": deadline.isoformat(timespec="seconds"),
        }
        s3.put_object(Bucket=acct.bucket, Key=f"{prefix}/run.json", Body=json.dumps(manifest, indent=2).encode())
        launched.append(manifest)
    return launched


def _schedule_reaper(scheduler, role_arn: str, run_id: str, instance_id: str, spot_request_id: str | None, deadline: datetime) -> None:
    def at(t: datetime) -> str:
        return f"at({t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')})"

    jobs = []
    if spot_request_id:
        jobs.append(("cancel", deadline + timedelta(minutes=5), "arn:aws:scheduler:::aws-sdk:ec2:cancelSpotInstanceRequests", {"SpotInstanceRequestIds": [spot_request_id]}))
    jobs.append(("terminate", deadline + timedelta(minutes=10), "arn:aws:scheduler:::aws-sdk:ec2:terminateInstances", {"InstanceIds": [instance_id]}))
    for kind, when, arn, payload in jobs:
        scheduler.create_schedule(
            Name=f"{run_id}-{kind}"[:64],
            GroupName=SCHEDULE_GROUP,
            ScheduleExpression=at(when),
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",
            Target={"Arn": arn, "RoleArn": role_arn, "Input": json.dumps(payload), "RetryPolicy": {"MaximumRetryAttempts": 10, "MaximumEventAgeInSeconds": 3600}},
            Description=f"flowcast training hard max runtime for {run_id}",
        )


# ---------------------------------------------------------------------- status / kill / fetch / cost


def training_instances(acct: Account, states=("pending", "running", "stopping", "stopped", "shutting-down")) -> list[dict]:
    ec2 = acct.client("ec2")
    filters = [{"Name": "tag:project", "Values": ["flowcast"]}, {"Name": "tag:component", "Values": ["training"]}, {"Name": "instance-state-name", "Values": list(states)}]
    out = []
    for page in ec2.get_paginator("describe_instances").paginate(Filters=filters):
        for r in page["Reservations"]:
            out.extend(r["Instances"])
    return out


def _read_json(s3, bucket: str, key: str) -> dict | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError:
        return None


def list_runs(acct: Account, prefix: str = "") -> list[dict]:
    s3 = acct.client("s3")
    runs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=acct.bucket, Prefix="runs/" + prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            run_id = cp["Prefix"].split("/")[1]
            manifest = _read_json(s3, acct.bucket, f"runs/{run_id}/run.json") or {"run_id": run_id}
            manifest["status"] = _read_json(s3, acct.bucket, f"runs/{run_id}/status.json")
            runs.append(manifest)
    live = {tagv(i, "flowcast:run"): i for i in training_instances(acct)}
    for r in runs:
        inst = live.get(r["run_id"])
        r["instance_state"] = inst["State"]["Name"] if inst else "gone"
    return runs


def tagv(instance: dict, key: str) -> str | None:
    return next((t["Value"] for t in instance.get("Tags", []) if t["Key"] == key), None)


def kill(acct: Account, run_ids: list[str] | None = None) -> list[str]:
    """Cancel Spot requests and terminate instances for the given runs (all training runs if None)."""
    ec2 = acct.client("ec2")
    targets = [i for i in training_instances(acct) if run_ids is None or tagv(i, "flowcast:run") in run_ids]
    sirs = [i["SpotInstanceRequestId"] for i in targets if i.get("SpotInstanceRequestId")]
    if sirs:
        ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=sirs)
    ids = [i["InstanceId"] for i in targets]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
    return ids


def fetch(acct: Account, run_id: str, dest: Path, include_checkpoints: bool = False) -> Path:
    dest = Path(dest) / run_id
    cmd = ["aws", "s3", "sync", f"s3://{acct.bucket}/runs/{run_id}", str(dest), "--only-show-errors", "--exclude", "run/optimizer_state_*", "--exclude", "run/rng_state_*"]
    if not include_checkpoints:
        cmd += ["--exclude", "run/model_epoch*"]
    subprocess.run(cmd, check=True)
    return dest


def run_cost(acct: Account, run_id: str) -> dict:
    """Estimated cost from the instance's heartbeat log (one line per running minute) and Spot price history."""
    s3, ec2 = acct.client("s3"), acct.client("ec2")
    manifest = _read_json(s3, acct.bucket, f"runs/{run_id}/run.json") or {}
    try:
        body = s3.get_object(Bucket=acct.bucket, Key=f"runs/{run_id}/heartbeat.jsonl")["Body"].read().decode()
    except ClientError:
        body = ""
    beats = [json.loads(line) for line in body.splitlines() if line.strip()]
    boots = len({b.get("boot") for b in beats}) or (1 if manifest else 0)
    running_h = (len(beats) + 3 * boots) / 60.0  # +3 min per boot for pending/boot before the first heartbeat
    first = datetime.fromisoformat(beats[0]["time"]) if beats else datetime.fromisoformat(manifest["launched"]) if manifest.get("launched") else datetime.now(timezone.utc)
    last = datetime.fromisoformat(beats[-1]["time"]) if beats else first
    az, itype = manifest.get("availability_zone"), manifest.get("instance_type")
    price = None
    if az and itype:
        hist = ec2.describe_spot_price_history(InstanceTypes=[itype], ProductDescriptions=["Linux/UNIX"], AvailabilityZone=az, StartTime=first - timedelta(hours=1), EndTime=last + timedelta(minutes=5))["SpotPriceHistory"]
        if hist:
            price = sum(float(h["SpotPrice"]) for h in hist) / len(hist)
    wall_h = max((last - first).total_seconds() / 3600.0, running_h)
    ebs = EBS_GB * EBS_USD_PER_GB_MONTH / 730.0 * wall_h
    ipv4 = PUBLIC_IPV4_USD_PER_H * running_h
    compute = (price or 0.0) * running_h
    return {"run_id": run_id, "instance_type": itype, "az": az, "boots": boots, "running_h": round(running_h, 3), "spot_usd_per_h": price, "compute_usd": round(compute, 3), "ebs_usd": round(ebs, 3), "ipv4_usd": round(ipv4, 3), "total_usd": round(compute + ebs + ipv4, 3)}


def upload_dataset(acct: Account, src: Path, name: str) -> str:
    uri = f"s3://{acct.bucket}/datasets/{name}"
    subprocess.run(["aws", "s3", "sync", str(src), f"{uri}/{Path(src).name}", "--only-show-errors"], check=True)
    return f"{uri}/{Path(src).name}"

