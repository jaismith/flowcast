"""EC2 Spot training runs: shared resources, launch, status, kill, cost.

Every resource is tagged `project=flowcast component=training`. A run is one persistent Spot instance (interruption
behavior `stop`, so an interrupted instance keeps its EBS root and resumes when capacity returns) that pulls the
code and dataset, trains with per-epoch checkpoints synced to S3, hindcasts the validation years, uploads results
and terminates itself. Three independent limits stop anything from running past `max_hours`:

1. the Spot request's `ValidUntil` (no relaunch after the deadline);
2. an on-instance watchdog (graceful stop 15 min before, then sync and self-terminate at the deadline);
3. two EventBridge Scheduler one-shot schedules that cancel the Spot request and terminate the instance a few
   minutes after the deadline, even if the instance is wedged.

`on_demand=True` launches an On-Demand instance instead (opt-in, for when Spot capacity keeps failing): no Spot
request, so limit 1 doesn't apply and an OS shutdown terminates the instance; limits 2 and 3 are unchanged.
"""

from __future__ import annotations

import json
import logging
import math
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
GPU_AMI_PARAMETER = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
CPU_AMI_PARAMETER = "/aws/service/canonical/ubuntu/server/24.04/stable/current/{arch}/hvm/ebs-gp3/ami-id"
EBS_GB = 100
EBS_USD_PER_GB_MONTH = 0.08
PUBLIC_IPV4_USD_PER_H = 0.005
# Linux On-Demand list prices (USD/h) in the regions the launcher uses; for cost estimates of On-Demand runs
ON_DEMAND_USD_PER_H = {
    "g4dn.xlarge": 0.526, "g4dn.2xlarge": 0.752, "g5.xlarge": 1.006, "g5.2xlarge": 1.212,
    "g6.xlarge": 0.805, "g6.2xlarge": 0.978, "g6e.xlarge": 1.861, "g6e.2xlarge": 2.242,
}
CAPACITY_ERRORS = ("InsufficientInstanceCapacity", "SpotMaxPriceTooLow", "Unsupported", "InsufficientCapacity", "MaxSpotInstanceCountExceeded", "VcpuLimitExceeded", "InstanceLimitExceeded")


class NoCapacityError(RuntimeError):
    pass


def tag_list(extra: dict | None = None) -> list[dict]:
    return [{"Key": k, "Value": str(v)} for k, v in {**TAGS, **(extra or {})}.items()]


CANDIDATE_REGIONS = ("us-east-2", "us-west-2")
G_SPOT_QUOTA = "L-3819A6DF"
HOME_SERVICES = {"s3", "iam", "sts"}


@dataclass
class Account:
    """Home region: the S3 bucket for code, configs, checkpoints and results. Compute region: EC2, Scheduler, AMIs."""

    session: boto3.Session = field(default_factory=boto3.Session)
    region: str | None = None

    def __post_init__(self):
        self.home_region = self.session.region_name
        self.region = self.region or self.home_region
        self.account_id = self.session.client("sts").get_caller_identity()["Account"]
        self.bucket = f"{NAME}-{self.account_id}"
        self.dataset_bucket = f"flowcast-dataset-{self.account_id}"

    def client(self, name: str, region: str | None = None):
        return self.session.client(name, region_name=region or (self.home_region if name in HOME_SERVICES else self.region))

    def in_region(self, region: str) -> "Account":
        return Account(self.session, region)

    @property
    def replica_bucket(self) -> str:
        return self.bucket if self.region == self.home_region else f"{self.bucket}-{self.region}"


def bucket_region(acct: Account, bucket: str) -> str:
    try:
        meta = acct.client("s3").head_bucket(Bucket=bucket)["ResponseMetadata"]
    except ClientError as err:
        meta = err.response["ResponseMetadata"]
    region = meta.get("HTTPHeaders", {}).get("x-amz-bucket-region")
    if not region:
        raise RuntimeError(f"cannot determine the region of bucket {bucket}")
    return region


def split_s3(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, key


def s3_prefix_bytes(acct: Account, uri: str, region: str) -> int:
    bucket, key = split_s3(uri)
    pages = acct.client("s3", region).get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key.rstrip("/") + "/")
    return sum(obj["Size"] for page in pages for obj in page.get("Contents", []))


def instance_storage_gb(acct: Account, instance_type: str) -> float:
    info = acct.client("ec2").describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]
    return float(info.get("InstanceStorageInfo", {}).get("TotalSizeInGB", 0))


def data_placement(acct: Account, dataset_uris: list[str], dataset_regions: list[str], instance_type: str, force_ebs: bool = False) -> tuple[int, bool]:
    """(EBS root size in GB, whether bootstrap.sh must cache the datasets on the root volume).

    Same-region datasets go to instance NVMe when it holds them with 10% headroom (a 128 GB cube does not fit a
    g4dn.xlarge's 125 GB); cross-region datasets always go to the root volume. `force_ebs` keeps them on the root
    volume anyway: NVMe is wiped when a Spot instance stops, so every restart would copy the dataset again.
    """
    sizes = [s3_prefix_bytes(acct, u, r) for u, r in zip(dataset_uris, dataset_regions)]
    cross = any(r != acct.region for r in dataset_regions)
    on_ebs = force_ebs or cross or instance_storage_gb(acct, instance_type) < sum(sizes) * 1.1 / 1e9
    return (EBS_GB + math.ceil(sum(sizes) * 1.1 / 1e9) if on_ebs else EBS_GB), on_ebs


def spot_quota_vcpus(acct: Account, region: str) -> float:
    return float(acct.client("service-quotas", region).get_service_quota(ServiceCode="ec2", QuotaCode=G_SPOT_QUOTA)["Quota"]["Value"])


def instance_vcpus(acct: Account, region: str, instance_type: str) -> int:
    return int(acct.client("ec2", region).describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]["VCpuInfo"]["DefaultVCpus"])


def pick_region(acct: Account, instance_type: str, count: int, candidates: tuple[str, ...] | None = None) -> str:
    """Cheapest region whose Spot quota fits `count` instances (GPU types check the G/VT quota; CPU types use the home region)."""
    if not is_gpu(instance_type):
        return acct.region
    regions = [acct.home_region, *[r for r in (candidates or CANDIDATE_REGIONS) if r != acct.home_region]]
    options = []
    for r in regions:
        need = count * instance_vcpus(acct, r, instance_type)
        quota = spot_quota_vcpus(acct, r)
        hist = acct.client("ec2", r).describe_spot_price_history(InstanceTypes=[instance_type], ProductDescriptions=["Linux/UNIX"], StartTime=datetime.now(timezone.utc))["SpotPriceHistory"]
        price = min((float(h["SpotPrice"]) for h in hist), default=99.0)
        log.info("%s: G/VT Spot quota %.0f vCPUs (need %d), %s from $%.3f/h", r, quota, need, instance_type, price)
        if quota >= need:
            options.append((price, r))
    if not options:
        raise RuntimeError(f"no region in {regions} has G/VT Spot quota for {count} x {instance_type}")
    return min(options)[1]


GPU_PREFERENCE = ("g5.xlarge", "g6.xlarge", "g6.2xlarge", "g5.2xlarge")


def running_gpu_vcpus(acct: Account, region: str) -> int:
    ec2 = acct.client("ec2", region)
    filters = [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}, {"Name": "instance-type", "Values": ["g*", "vt*"]}]
    total = 0
    for page in ec2.get_paginator("describe_instances").paginate(Filters=filters):
        for r in page["Reservations"]:
            for i in r["Instances"]:
                total += i["CpuOptions"]["CoreCount"] * i["CpuOptions"]["ThreadsPerCore"]
    return total


def plan_gpu_slots(acct: Account, count: int, gpu_instance_types: tuple[str, ...] = GPU_PREFERENCE, regions: tuple[str, ...] | None = None) -> list[tuple[str, str]]:
    """Up to `count` (instance_type, region) GPU slots that fit the free G/VT Spot quota, cheapest first across regions."""
    regions = regions or (acct.home_region, *[r for r in CANDIDATE_REGIONS if r != acct.home_region])
    free = {r: spot_quota_vcpus(acct, r) - running_gpu_vcpus(acct, r) for r in regions}
    priced = []
    for r in regions:
        for itype in gpu_instance_types:
            hist = acct.client("ec2", r).describe_spot_price_history(InstanceTypes=[itype], ProductDescriptions=["Linux/UNIX"], StartTime=datetime.now(timezone.utc))["SpotPriceHistory"]
            if hist:
                priced.append((min(float(h["SpotPrice"]) for h in hist), itype, r, instance_vcpus(acct, r, itype)))
    slots = []
    for price, itype, r, vcpus in sorted(priced):
        while len(slots) < count and free[r] >= vcpus:
            slots.append((itype, r))
            free[r] -= vcpus
    return slots


def choose_instance(acct: Account, count: int, cpu_instance_type: str, gpu_instance_types: tuple[str, ...] = GPU_PREFERENCE) -> tuple[str, str]:
    """(instance_type, region): the cheapest GPU option whose Spot quota fits `count` runs, else the CPU type at home."""
    options = []
    for itype in gpu_instance_types:
        try:
            region = pick_region(acct, itype, count)
        except RuntimeError:
            continue
        hist = acct.client("ec2", region).describe_spot_price_history(InstanceTypes=[itype], ProductDescriptions=["Linux/UNIX"], StartTime=datetime.now(timezone.utc))["SpotPriceHistory"]
        options.append((min((float(h["SpotPrice"]) for h in hist), default=99.0), itype, region))
    if options:
        _, itype, region = min(options)
        return itype, region
    return cpu_instance_type, acct.home_region


def _replica_marker(uri: str) -> str:
    return f"_replicas/{split_s3(uri)[1].strip('/')}.json"


def source_etags(acct: Account, uri: str) -> dict[str, str]:
    bucket, prefix = split_s3(uri)
    s3 = acct.client("s3", bucket_region(acct, bucket))
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/")
    return {o["Key"]: o["ETag"].strip('"') for page in pages for o in page.get("Contents", [])}


def replica_for(acct: Account, uri: str) -> tuple[str, str | None]:
    """Where a run in `acct.region` reads a dataset from, and where it should publish a replica (or None).

    A dataset in another region is pulled across regions by every run that reads it directly (about $0.01-0.02/GB).
    The first such run also uploads its local copy to a bucket in its region (free) with a marker of the source's
    object ETags; later runs read that replica while the marker still matches the source, so a dataset updated in
    place falls back to the source and gets its replica refreshed.
    """
    bucket, key = split_s3(uri)
    if bucket_region(acct, bucket) == acct.region:
        return uri, None
    s3 = acct.client("s3", acct.region)
    replica = acct.replica_bucket
    try:
        s3.head_bucket(Bucket=replica)
    except ClientError:
        s3.create_bucket(Bucket=replica, CreateBucketConfiguration={"LocationConstraint": acct.region})
        s3.put_public_access_block(Bucket=replica, PublicAccessBlockConfiguration={k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
        s3.put_bucket_tagging(Bucket=replica, Tagging={"TagSet": tag_list()})
        log.info("created replica bucket %s in %s", replica, acct.region)
    target = f"s3://{replica}/{key}"
    try:
        marker = json.loads(s3.get_object(Bucket=replica, Key=_replica_marker(uri))["Body"].read())
    except ClientError:
        marker = None
    if marker is not None and marker == source_etags(acct, uri):
        return target, None
    log.info("no current replica of %s in %s: this run reads the source and publishes %s", uri, acct.region, target)
    return uri, target


# ---------------------------------------------------------------------- setup


def _ensure_bucket(acct: Account) -> None:
    s3 = acct.client("s3")
    try:
        s3.head_bucket(Bucket=acct.bucket)
    except ClientError:
        try:
            s3.create_bucket(Bucket=acct.bucket, CreateBucketConfiguration={"LocationConstraint": acct.home_region})
        except ClientError as err:
            # the S3 default region rejects an explicit location constraint
            if err.response["Error"]["Code"] != "InvalidLocationConstraint":
                raise
            s3.create_bucket(Bucket=acct.bucket)
        log.info("created bucket %s", acct.bucket)
    s3.put_public_access_block(Bucket=acct.bucket, PublicAccessBlockConfiguration={k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    s3.put_bucket_tagging(Bucket=acct.bucket, Tagging={"TagSet": tag_list()})
    _retry_conflict(lambda: s3.put_bucket_lifecycle_configuration(
        Bucket=acct.bucket,
        LifecycleConfiguration={
            "Rules": [
                {"ID": "abort-multipart", "Status": "Enabled", "Filter": {"Prefix": ""}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}},
                {"ID": "expire-code-bundles", "Status": "Enabled", "Filter": {"Prefix": "code/"}, "Expiration": {"Days": 60}},
            ]
        },
    ))


def _retry_conflict(call, attempts: int = 5) -> None:
    """Concurrent launches can race on bucket configuration (S3 OperationAborted)."""
    for i in range(attempts):
        try:
            call()
            return
        except ClientError as err:
            if err.response["Error"]["Code"] != "OperationAborted" or i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))


def _instance_policy(acct: Account) -> dict:
    buckets = [acct.bucket, f"{acct.bucket}-*", acct.dataset_bucket]
    tag_cond = {"StringEquals": {"aws:ResourceTag/project": "flowcast", "aws:ResourceTag/component": "training"}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{b}" for b in buckets]},
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{b}/*" for b in buckets]},
            {"Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject"], "Resource": [f"arn:aws:s3:::{acct.bucket}/*", f"arn:aws:s3:::{acct.bucket}-*/*"]},
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
    sg, vpc = _ensure_security_group(acct.client("ec2"))  # per compute region
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


def is_gpu(instance_type: str) -> bool:
    return instance_type.split(".")[0].rstrip("0123456789dnez").startswith(("g", "p"))


def instance_arch(acct: Account, instance_type: str) -> str:
    archs = acct.client("ec2").describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]["ProcessorInfo"]["SupportedArchitectures"]
    return "arm64" if "arm64" in archs and "x86_64" not in archs else "amd64"


def resolve_ami(acct: Account, instance_type: str) -> str:
    if is_gpu(instance_type):
        name = GPU_AMI_PARAMETER
    else:
        name = CPU_AMI_PARAMETER.format(arch=instance_arch(acct, instance_type))
    return acct.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]


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
    replicate: bool = True,
    on_demand: bool = False,
    data_on_ebs: bool = False,
    avoid_azs: tuple[str, ...] = (),
) -> list[dict]:
    res = setup(acct)
    publish = [None] * len(dataset_uris)
    if replicate:
        dataset_uris, publish = map(list, zip(*(replica_for(acct, u) for u in dataset_uris)))
    dataset_regions = [bucket_region(acct, split_s3(u)[0]) for u in dataset_uris]
    ebs_gb, data_on_ebs = data_placement(acct, dataset_uris, dataset_regions, instance_type, force_ebs=data_on_ebs)
    s3, ec2, scheduler = acct.client("s3"), acct.client("ec2"), acct.client("scheduler")
    code, code_id = package_code(repo)
    code_key = f"code/{code_id}.tar.gz"
    s3.put_object(Bucket=acct.bucket, Key=code_key, Body=code, Tagging="project=flowcast&component=training")
    ami = resolve_ami(acct, instance_type)
    # e.g. the zone of a sibling run, so one capacity reclaim doesn't stop both
    subnets = [row for row in _subnets_by_price(ec2, res["vpc"], instance_type) if row[1] not in avoid_azs]
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
            "S3_REGION": acct.home_region,
            "DATASET_REGIONS": " ".join(dataset_regions),
            "CODE_URI": f"s3://{acct.bucket}/{code_key}",
            "DATASET_URIS": " ".join(dataset_uris),
            "REPLICA_URIS": " ".join(p or "-" for p in publish),
            "DEADLINE_EPOCH": int(deadline.timestamp()),
            "MAX_BOOTS": 8,
            "REQUIRE_GPU": int(is_gpu(instance_type)),
            "DATA_ON_EBS": int(data_on_ebs),
        }
        user_data = _render_user_data(env)
        tags = {"Name": f"flowcast-train-{spec.run_id}"[:255], "flowcast:run": spec.run_id, "flowcast:deadline": deadline.isoformat(timespec="seconds")}
        if sweep:
            tags["flowcast:sweep"] = sweep
        market = {}
        if on_demand:
            market["InstanceInitiatedShutdownBehavior"] = "terminate"
        else:
            spot = {"SpotInstanceType": "persistent", "InstanceInterruptionBehavior": "stop", "ValidUntil": deadline}
            if max_price:
                spot["MaxPrice"] = f"{max_price:.4f}"
            market["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": spot}
        tagged = ("instance", "volume", "network-interface") if on_demand else ("instance", "volume", "spot-instances-request", "network-interface")
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
                    BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": ebs_gb, "VolumeType": "gp3", "DeleteOnTermination": True}}],
                    MetadataOptions={"HttpTokens": "required", "InstanceMetadataTags": "enabled", "HttpEndpoint": "enabled"},
                    UserData=user_data,
                    TagSpecifications=[{"ResourceType": r, "Tags": tag_list(tags)} for r in tagged],
                    **market,
                )["Instances"][0]
                if on_demand:
                    log.info("%s: launched %s in %s (On-Demand ~$%.3f/h)", spec.run_id, instance["InstanceId"], az, ON_DEMAND_USD_PER_H.get(instance_type, float("nan")))
                else:
                    log.info("%s: launched %s in %s (spot ~$%.3f/h)", spec.run_id, instance["InstanceId"], az, price)
                break
            except ClientError as err:
                code_ = err.response["Error"]["Code"]
                errors.append(f"{az}: {code_}")
                if code_ not in CAPACITY_ERRORS:
                    raise
        if instance is None:
            s3.delete_object(Bucket=acct.bucket, Key=f"{prefix}/config.yml")
            raise NoCapacityError(f"no {'On-Demand' if on_demand else 'Spot'} capacity or quota for {instance_type}: {errors}")
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
            "market": "on-demand" if on_demand else "spot",
            "region": acct.region,
            "availability_zone": instance["Placement"]["AvailabilityZone"],
            "ami": ami,
            "code": env["CODE_URI"],
            "datasets": dataset_uris,
            "ebs_gb": ebs_gb,
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
        params = dict(
            Name=f"{run_id}-{kind}"[:64],
            GroupName=SCHEDULE_GROUP,
            ScheduleExpression=at(when),
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",
            Target={"Arn": arn, "RoleArn": role_arn, "Input": json.dumps(payload), "RetryPolicy": {"MaximumRetryAttempts": 10, "MaximumEventAgeInSeconds": 3600}},
            Description=f"flowcast training hard max runtime for {run_id}",
        )
        try:
            scheduler.create_schedule(**params)
        except scheduler.exceptions.ConflictException:  # a relaunched run replaces its old deadline
            scheduler.update_schedule(**params)


# ---------------------------------------------------------------------- status / kill / fetch / cost


def training_instances(acct: Account, states=("pending", "running", "stopping", "stopped", "shutting-down"), regions: list[str] | None = None) -> list[dict]:
    if regions:
        return [i for r in dict.fromkeys(regions) for i in training_instances(acct.in_region(r), states)]
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
    regions = [acct.region, *[r["region"] for r in runs if r.get("region")]]
    live = {tagv(i, "flowcast:run"): i for i in training_instances(acct, regions=regions)}
    for r in runs:
        inst = live.get(r["run_id"])
        r["instance_state"] = inst["State"]["Name"] if inst else "gone"
    return runs


def has_checkpoint(acct: Account, run_id: str) -> bool:
    return _read_json(acct.client("s3"), acct.bucket, f"runs/{run_id}/run/checkpoint.json") is not None


def tagv(instance: dict, key: str) -> str | None:
    return next((t["Value"] for t in instance.get("Tags", []) if t["Key"] == key), None)


def kill(acct: Account, run_ids: list[str] | None = None, regions: list[str] | None = None) -> list[str]:
    """Cancel Spot requests and terminate instances for the given runs (all training runs if None)."""
    killed = []
    for region in dict.fromkeys(regions or [acct.region, *CANDIDATE_REGIONS]):
        ec2 = acct.client("ec2", region)
        targets = [i for i in training_instances(acct.in_region(region)) if run_ids is None or tagv(i, "flowcast:run") in run_ids]
        sirs = [i["SpotInstanceRequestId"] for i in targets if i.get("SpotInstanceRequestId")]
        if sirs:
            ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=sirs)
        ids = [i["InstanceId"] for i in targets]
        if ids:
            ec2.terminate_instances(InstanceIds=ids)
        for inst in targets:
            status = {"status": "killed", "detail": "stopped with flowcast-train kill", "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "instance": inst["InstanceId"]}
            acct.client("s3").put_object(Bucket=acct.bucket, Key=f"runs/{tagv(inst, 'flowcast:run')}/status.json", Body=json.dumps(status).encode())
        killed += ids
    return killed


def fetch(acct: Account, run_id: str, dest: Path, include_checkpoints: bool = False) -> Path:
    dest = Path(dest) / run_id
    cmd = ["aws", "s3", "sync", f"s3://{acct.bucket}/runs/{run_id}", str(dest), "--only-show-errors", "--exclude", "run/optimizer_state_*", "--exclude", "run/rng_state_*"]
    if not include_checkpoints:
        cmd += ["--exclude", "run/model_epoch*"]
    subprocess.run(cmd, check=True)
    return dest


def run_cost(acct: Account, run_id: str) -> dict:
    """Estimated cost from the instance's heartbeat log (one line per running minute) and Spot price history
    (On-Demand runs: the list price; `spot_usd_per_h` then holds that price)."""
    s3 = acct.client("s3")
    manifest = _read_json(s3, acct.bucket, f"runs/{run_id}/run.json") or {}
    ec2 = acct.client("ec2", manifest.get("region") or acct.region)
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
    if manifest.get("market") == "on-demand":
        price = ON_DEMAND_USD_PER_H.get(itype)
    elif az and itype:
        hist = ec2.describe_spot_price_history(InstanceTypes=[itype], ProductDescriptions=["Linux/UNIX"], AvailabilityZone=az, StartTime=first - timedelta(hours=1), EndTime=last + timedelta(minutes=5))["SpotPriceHistory"]
        if hist:
            price = sum(float(h["SpotPrice"]) for h in hist) / len(hist)
    wall_h = max((last - first).total_seconds() / 3600.0, running_h)
    ebs = manifest.get("ebs_gb", EBS_GB) * EBS_USD_PER_GB_MONTH / 730.0 * wall_h
    ipv4 = PUBLIC_IPV4_USD_PER_H * running_h
    compute = (price or 0.0) * running_h
    return {"run_id": run_id, "instance_type": itype, "az": az, "boots": boots, "running_h": round(running_h, 3), "market": manifest.get("market", "spot"), "spot_usd_per_h": price, "compute_usd": round(compute, 3), "ebs_usd": round(ebs, 3), "ipv4_usd": round(ipv4, 3), "total_usd": round(compute + ebs + ipv4, 3)}


def upload_dataset(acct: Account, src: Path, name: str) -> str:
    uri = f"s3://{acct.bucket}/datasets/{name}"
    subprocess.run(["aws", "s3", "sync", str(src), f"{uri}/{Path(src).name}", "--only-show-errors"], check=True)
    return f"{uri}/{Path(src).name}"

