"""Run extraction shards on a fleet of one-time EC2 Spot instances.

Every instance gets a hard runtime cap (`shutdown -h +N` at boot with shutdown behaviour = terminate), and
shuts down itself when its jobs finish, so nothing lingers. Shards are idempotent: a relaunch skips shards
whose outputs already exist in S3, so a Spot interruption only costs a rerun of that instance's shards.
"""

import json
import logging
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
import numpy as np

from . import extract
from .config import AWS_REGION, BUCKET, TAGS
from .sources import SOURCES

log = logging.getLogger(__name__)

# Rough single-process seconds per task next to the data (measured outside AWS, then discounted).
TASK_SECONDS = {"aorc": 2.0, "hrrr_forecast": 2.0, "hrrr_analysis": 1.5, "mrms": 0.8, "gefs_forecast": 1.0}
INSTANCE_TYPES = ("r7i.2xlarge", "r6i.2xlarge", "m7i.2xlarge", "r7a.2xlarge", "m6i.2xlarge")
AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
PROFILE = "flowcast-dataset-extract"
SECURITY_GROUP = "flowcast-dataset-extract"


@dataclass
class Assignment:
    index: int
    jobs: list[tuple[str, int]]
    cost_s: float
    mem_gb: float


def shard_cost(plan: extract.Plan, shard_i: int) -> tuple[float, float]:
    src = SOURCES[plan.source]
    shard = plan.shards[shard_i]
    n_tasks = len(extract.tasks_for(plan, shard_i, "A")) + len(extract.tasks_for(plan, shard_i, "B"))
    shape = (*extract.leading_shape(src, shard.stop - shard.start), len(plan.units), len(src.outputs))
    return n_tasks * TASK_SECONDS[plan.source], (1 if src.strict else 2) * 4 * float(np.prod(shape)) / 1e9


def assign(plans: dict[str, extract.Plan], n_instances: int, mem_cap_gb: float = 48.0, skip: set[tuple[str, str]] = frozenset()) -> list[Assignment]:
    """Longest-processing-time packing of shards onto instances, respecting accumulator memory."""
    items = []
    for name, plan in plans.items():
        for i, shard in enumerate(plan.shards):
            if (name, shard.shard_id) in skip:
                continue
            cost, mem = shard_cost(plan, i)
            items.append((cost, mem, name, i))
    items.sort(reverse=True)
    bins = [Assignment(k, [], 0.0, 0.0) for k in range(n_instances)]
    for cost, mem, name, i in items:
        fits = [b for b in bins if b.mem_gb + mem <= mem_cap_gb] or bins
        b = min(fits, key=lambda b: b.cost_s)
        b.jobs.append((name, i))
        b.cost_s += cost
        b.mem_gb += mem
    return [b for b in bins if b.jobs]


def bundle_code(repo_root: Path, out: Path) -> str:
    sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "pipeline"], cwd=repo_root, capture_output=True, text=True).stdout.strip()
    name = f"{sha}{'-dirty' if dirty else ''}.tar.gz"
    with tarfile.open(out / name, "w:gz") as tar:
        tar.add(repo_root / "pipeline", arcname="pipeline", filter=lambda t: None if "/.venv" in t.name or "__pycache__" in t.name else t)
    return name


USER_DATA = """#!/bin/bash
set -uxo pipefail
shutdown -h +{max_minutes}
exec > /var/log/flowcast.log 2>&1
IMDS=$(curl -sX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
export AWS_REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS" http://169.254.169.254/latest/meta-data/placement/region)
export AWS_DEFAULT_REGION=$AWS_REGION
B={bucket}; RUN={run}; I={index}
( while true; do sleep 120; aws s3 cp /var/log/flowcast.log s3://$B/logs/$RUN/$I.log --only-show-errors; done ) &
mkdir -p /opt/fc && cd /opt/fc
aws s3 cp s3://$B/work/code/{bundle} code.tar.gz --only-show-errors && tar xzf code.tar.gz
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
cd /opt/fc/pipeline && UV_PYTHON_INSTALL_DIR=/opt/uv-python uv sync --python 3.12 --extra dataset --frozen --no-dev
aws s3 cp s3://$B/work/runs/$RUN/jobs/$I.json /opt/fc/job.json --only-show-errors
aws s3 sync s3://$B/work/runs/$RUN/plans/ /opt/fc/plans/ --only-show-errors
OMP_NUM_THREADS=1 /opt/fc/pipeline/.venv/bin/flowcast-dataset extract-worker --job /opt/fc/job.json --plans /opt/fc/plans --out /opt/fc/out
echo "worker exit $?"
aws s3 cp /var/log/flowcast.log s3://$B/logs/$RUN/$I.log --only-show-errors
shutdown -h now
"""


def _tag_spec(resource: str, extra: dict[str, str]) -> dict:
    return {"ResourceType": resource, "Tags": [{"Key": k, "Value": v} for k, v in {**TAGS, **extra}.items()]}


def launch(run: str, assignments: list[Assignment], plan_dir: Path, bundle: Path, workers: int, max_minutes: int) -> list[str]:
    s3 = boto3.client("s3", region_name=AWS_REGION)
    ec2 = boto3.client("ec2", region_name=AWS_REGION)
    ssm = boto3.client("ssm", region_name=AWS_REGION)
    s3.upload_file(str(bundle), BUCKET, f"work/code/{bundle.name}")
    for p in plan_dir.glob("*.pkl"):
        s3.upload_file(str(p), BUCKET, f"work/runs/{run}/plans/{p.name}")
    ami = ssm.get_parameter(Name=AMI_PARAM)["Parameter"]["Value"]
    sg = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SECURITY_GROUP]}])["SecurityGroups"][0]["GroupId"]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])["Subnets"]]
    ids = []
    for a in assignments:
        job = {"run": run, "bucket": BUCKET, "index": a.index, "workers": workers, "jobs": a.jobs}
        s3.put_object(Bucket=BUCKET, Key=f"work/runs/{run}/jobs/{a.index}.json", Body=json.dumps(job).encode())
        user_data = USER_DATA.format(max_minutes=max_minutes, bucket=BUCKET, run=run, index=a.index, bundle=bundle.name)
        ids.append(_run_spot(ec2, ami, sg, subnets, user_data, {"Name": f"flowcast-dataset-{run}-{a.index}", "run": run}))
        log.info("instance %d -> %s (%d shards, est %.0f core-min, %.1f GB)", a.index, ids[-1], len(a.jobs), a.cost_s / 60, a.mem_gb)
    return ids


def _run_spot(ec2, ami: str, sg: str, subnets: list[str], user_data: str, tags: dict[str, str], quota_wait_s: int = 3600) -> str:
    """Launch one Spot instance, waiting (up to `quota_wait_s`) while the account's Spot vCPU quota is in use."""
    deadline = time.time() + quota_wait_s
    while True:
        try:
            return _try_spot(ec2, ami, sg, subnets, user_data, tags)
        except ec2.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] != "MaxSpotInstanceCountExceeded" or time.time() > deadline:
                raise
            log.info("Spot vCPU quota in use; retrying in 60 s")
            time.sleep(60)


def _try_spot(ec2, ami: str, sg: str, subnets: list[str], user_data: str, tags: dict[str, str]) -> str:
    last = None
    for itype in INSTANCE_TYPES:
        for subnet in subnets:
            try:
                resp = ec2.run_instances(
                    ImageId=ami,
                    InstanceType=itype,
                    MinCount=1,
                    MaxCount=1,
                    SubnetId=subnet,
                    SecurityGroupIds=[sg],
                    IamInstanceProfile={"Name": PROFILE},
                    InstanceInitiatedShutdownBehavior="terminate",
                    InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}},
                    BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 60, "VolumeType": "gp3", "DeleteOnTermination": True}}],
                    MetadataOptions={"HttpTokens": "required"},
                    UserData=user_data,
                    TagSpecifications=[_tag_spec("instance", tags), _tag_spec("volume", tags), _tag_spec("spot-instances-request", tags)],
                )
                return resp["Instances"][0]["InstanceId"]
            except ec2.exceptions.ClientError as exc:
                code = exc.response["Error"]["Code"]
                last = exc
                if code in ("InsufficientInstanceCapacity", "SpotMaxPriceTooLow", "Unsupported", "InvalidParameterCombination"):
                    continue
                raise
    raise RuntimeError(f"no Spot capacity: {last}")


def done_shards(run: str) -> set[tuple[str, str]]:
    s3 = boto3.client("s3", region_name=AWS_REGION)
    out = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"work/runs/{run}/extract/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            if parts[-1] == "all.npy.zst":
                out.add((parts[-3], parts[-2]))
    return out


def run_worker(job_path: Path, plan_dir: Path, out_dir: Path) -> None:
    job = json.loads(job_path.read_text())
    s3 = boto3.client("s3", region_name=AWS_REGION)
    run, bucket = job["run"], job["bucket"]
    plans = {name for name, _ in job["jobs"]}
    plan_paths = {name: str(plan_dir / f"{name}.pkl") for name in plans}
    finished = done_shards(run)
    extract._init_worker(plan_paths)
    jobs = [(s, i) for s, i in job["jobs"] if (s, extract._PLANS[s].shards[i].shard_id) not in finished]
    log.info("%d jobs (%d already done)", len(jobs), len(job["jobs"]) - len(jobs))

    def upload(path: Path) -> None:
        key = f"work/runs/{run}/extract/{path.relative_to(out_dir)}"
        s3.upload_file(str(path), bucket, key)

    t0 = time.time()
    extract.run_jobs(jobs, plan_paths, out_dir, job["workers"], upload=upload)
    log.info("worker finished in %.1f min", (time.time() - t0) / 60)
