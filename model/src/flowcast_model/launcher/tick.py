"""One tick of the Spot watcher, run every few minutes by a scheduled Lambda (`flowcast-train tick-deploy`).

Agent VMs pause while idle, so relaunch loops can't live there. The plan (s3://<bucket>/_tick/plan.json) lists
runs, each with the instance types to try in order ([type, max_hours, max_price]) and optionally runs whose
hindcasts must exist first (`after`). Per tick and run:

* hindcast present (run/hindcast/_hindcast.json): done;
* an instance exists: nothing, unless it has been Spot-stopped longer than `stopped_grace_min` (default 15), then
  it is killed so the next tick can place the run in any zone;
* a job with "region" is looked up, killed and relaunched in that region (default: the home region);
* no instance, status not `failed`, prerequisites done: relaunch on Spot from the run's checkpoint (launch.json or
  run.json in S3), first instance type with capacity wins, avoiding sibling runs' zones on alternate ticks.

With `upgrade` ({"from": [types], "to": [[type, max_hours, max_price], ...]}), a run staging or training on a slow
type moves to a faster one as soon as one launches (new instance first, then the old one is terminated).

Runs whose ID ends in `-pc` are trained off AWS and are skipped entirely, even if a plan lists them.

A failed run (status `failed`, e.g. an OOM after the guard's one restart) needs a person and is left alone, and
so is a run whose config.yml is missing. Once every run is done the tick disables its own schedule. Spot only.
The Lambda's reserved concurrency of 1 and no async retries keep ticks from overlapping, so a run is never launched
twice; a tick starts no new launch attempt after 10 minutes (Lambda timeout 15).
"""

from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

from . import aws

log = logging.getLogger(__name__)

PLAN_KEY = "_tick/plan.json"
STATE_KEY = "_tick/state.json"
TICK_NAME = "flowcast-training-tick"
INVOKE_ROLE = "flowcast-training-tick-invoke"
EXTERNAL_SUFFIX = "-pc"
LIVE_STATES = ["pending", "running", "stopping", "stopped", "shutting-down"]


def read_plan(acct: aws.Account) -> dict | None:
    return aws._read_json(acct.client("s3"), acct.bucket, PLAN_KEY)


def hindcast_done(acct: aws.Account, run_id: str) -> bool:
    try:
        acct.client("s3").head_object(Bucket=acct.bucket, Key=f"runs/{run_id}/run/hindcast/_hindcast.json")
        return True
    except ClientError:
        return False


def run_status(acct: aws.Account, run_id: str) -> str | None:
    status = aws._read_json(acct.client("s3"), acct.bucket, f"runs/{run_id}/status.json") or {}
    return status.get("status")


def instances(acct: aws.Account, run_id: str) -> list[dict]:
    res = acct.client("ec2").describe_instances(Filters=[{"Name": "tag:flowcast:run", "Values": [run_id]}, {"Name": "instance-state-name", "Values": LIVE_STATES}])
    return [i for r in res["Reservations"] for i in r["Instances"]]


def stopped_seconds(instance: dict, now: datetime) -> float | None:
    """How long an instance has been stopped, from its state-transition reason ('... (2026-09-28 17:02:14 GMT)')."""
    if instance["State"]["Name"] != "stopped":
        return None
    reason = instance.get("StateTransitionReason", "")
    try:
        when = datetime.strptime(reason[reason.rindex("(") + 1 : reason.rindex(")")], "%Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (now - when).total_seconds()


def upgrade(acct: aws.Account, run_id: str, old: dict, spec: dict, res: dict) -> str | None:
    """Move a run that is staging or training on a slow type (spec["from"]) to a faster one (spec["to"]) without losing
    work: launch the new instance first (it restores the latest checkpoint from S3 after its own dataset copy), and only
    then terminate the old one. With no capacity or quota for the faster types nothing changes."""
    if old["State"]["Name"] != "running" or old["InstanceType"] not in spec["from"] or run_status(acct, run_id) not in ("staging", "training"):
        return None
    for itype, hours, price in spec["to"]:
        try:
            manifest = aws.relaunch(acct, run_id, itype, hours, price, res=res)
        except aws.NoCapacityError:
            continue
        ec2 = acct.client("ec2")
        if old.get("SpotInstanceRequestId"):
            ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=[old["SpotInstanceRequestId"]])
        ec2.terminate_instances(InstanceIds=[old["InstanceId"]])
        return f"upgraded {old['InstanceType']} -> {itype} in {manifest['availability_zone']}"
    return None


def disable_schedule(acct: aws.Account) -> None:
    scheduler = acct.client("scheduler")
    try:
        current = scheduler.get_schedule(Name=TICK_NAME, GroupName=aws.SCHEDULE_GROUP)
    except scheduler.exceptions.ResourceNotFoundException:
        return
    keep = ("Name", "GroupName", "ScheduleExpression", "ScheduleExpressionTimezone", "FlexibleTimeWindow", "Target", "Description")
    scheduler.update_schedule(**{k: current[k] for k in keep if k in current}, State="DISABLED")


def tick(acct: aws.Account, now: datetime | None = None, budget_s: float = 600) -> dict:
    """`budget_s`: no new launch attempt starts after this long (each failed Spot attempt takes up to a minute)."""
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    plan = read_plan(acct)
    if not plan or not plan.get("enabled", True):
        return {"time": now.isoformat(timespec="seconds"), "enabled": False}
    # runs trained off AWS (run IDs ending in -pc, e.g. on a workstation) are never launched, killed or relaunched here
    external = [j["run_id"] for j in plan["jobs"] if j["run_id"].endswith(EXTERNAL_SUFFIX)]
    jobs = [j for j in plan["jobs"] if not j["run_id"].endswith(EXTERNAL_SUFFIX)]
    # a job may run outside the home region ("region"); its instances are looked up, killed and relaunched there
    where = {j["run_id"]: acct.in_region(j["region"]) if j.get("region") else acct for j in jobs}
    done = {j["run_id"]: hindcast_done(acct, j["run_id"]) for j in jobs}
    live = {j["run_id"]: instances(where[j["run_id"]], j["run_id"]) for j in jobs}
    actions: dict[str, str] = {rid: "external host: not managed by the tick" for rid in external}
    resources: dict[str, dict] = {}
    for job in jobs:
        rid = job["run_id"]
        region_acct = where[rid]
        if done[rid]:
            actions[rid] = "done"
            continue
        if live[rid]:
            down = stopped_seconds(live[rid][0], now)
            if down is not None and down > 60 * job.get("stopped_grace_min", 15):
                aws.kill(region_acct, [rid], regions=[region_acct.region])
                actions[rid] = f"killed after {down / 60:.0f} min Spot-stopped"
            elif job.get("upgrade") and len(live[rid]) == 1:
                if region_acct.region not in resources:
                    resources[region_acct.region] = aws.lookup_resources(region_acct)
                actions[rid] = upgrade(region_acct, rid, live[rid][0], job["upgrade"], resources[region_acct.region]) or live[rid][0]["State"]["Name"]
            else:
                actions[rid] = live[rid][0]["State"]["Name"]
            continue
        if run_status(acct, rid) == "failed":
            actions[rid] = "failed: left for a person"
            continue
        waiting = [d for d in job.get("after", []) if not hindcast_done(acct, d)]
        if waiting:
            actions[rid] = f"waiting for {', '.join(waiting)}"
            continue
        # avoid sibling runs' zones on alternate ticks, so one zone with capacity can still take every run
        avoid = ()
        if (now.minute // 5) % 2:
            avoid = tuple(sorted({i["Placement"]["AvailabilityZone"] for other, insts in live.items() if other != rid for i in insts}))
        if region_acct.region not in resources:
            resources[region_acct.region] = aws.lookup_resources(region_acct)
        res = resources[region_acct.region]
        actions[rid] = "no capacity"
        for itype, hours, price in job["types"]:
            if time.monotonic() - started > budget_s:
                actions[rid] = "no capacity (tick time budget used)"
                break
            try:
                manifest = aws.relaunch(region_acct, rid, itype, hours, price, avoid, data_on_ebs=job.get("data_on_ebs", True), res=res)
            except aws.NoCapacityError:
                continue
            except aws.MissingConfigError:
                actions[rid] = "config.yml missing: needs a person"
                break
            live[rid] = instances(region_acct, rid)
            actions[rid] = f"launched {itype} in {manifest['availability_zone']}"
            break
    state = {"time": now.isoformat(timespec="seconds"), "actions": actions}
    if all(done.values()):
        state["finished"] = True
        acct.client("s3").put_object(Bucket=acct.bucket, Key=PLAN_KEY, Body=json.dumps({**plan, "enabled": False}, indent=2).encode())
    acct.client("s3").put_object(Bucket=acct.bucket, Key=STATE_KEY, Body=json.dumps(state, indent=2).encode())
    if state.get("finished"):
        try:
            disable_schedule(acct)
        except ClientError as err:  # a disabled plan already makes every later tick a no-op
            log.warning("could not disable the tick schedule: %s", err.response["Error"]["Code"])
    log.info("tick: %s", json.dumps(state))
    return state


def handler(event, context):
    logging.getLogger().setLevel(logging.INFO)
    return tick(aws.Account(boto3.Session()))


# ---------------------------------------------------------------------- deploy


def lambda_zip() -> bytes:
    """The launcher package (aws.py, tick.py, bootstrap.sh) plus PyYAML; boto3 comes with the Lambda runtime."""
    pkg = Path(__file__).resolve().parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("flowcast_model/__init__.py", "")
        for name in ("__init__.py", "aws.py", "tick.py", "bootstrap.sh"):
            z.write(pkg / name, f"flowcast_model/launcher/{name}")
        root = Path(yaml.__file__).resolve().parent
        for f in root.rglob("*.py"):
            z.write(f, f"yaml/{f.relative_to(root)}")
    return buf.getvalue()


def _tick_policy(acct: aws.Account) -> dict:
    tag_cond = {"StringEquals": {"aws:ResourceTag/project": "flowcast", "aws:ResourceTag/component": "training"}}
    iam_arn = f"arn:aws:iam::{acct.account_id}:role"
    buckets = [acct.bucket, acct.dataset_bucket]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{b}" for b in buckets]},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": [f"arn:aws:s3:::{acct.bucket}/runs/*", f"arn:aws:s3:::{acct.bucket}/_tick/*"]},
            {"Effect": "Allow", "Action": ["ec2:Describe*", "ec2:RunInstances", "ec2:CreateTags"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["ec2:TerminateInstances", "ec2:CancelSpotInstanceRequests"], "Resource": "*", "Condition": tag_cond},
            # the invoke role too: disabling its own schedule passes the schedule's role again
            {"Effect": "Allow", "Action": ["iam:PassRole"], "Resource": [f"{iam_arn}/{aws.INSTANCE_ROLE}", f"{iam_arn}/{aws.REAPER_ROLE}", f"{iam_arn}/{INVOKE_ROLE}"]},
            {"Effect": "Allow", "Action": ["iam:GetRole"], "Resource": f"{iam_arn}/{aws.REAPER_ROLE}"},
            {"Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["scheduler:GetSchedule", "scheduler:CreateSchedule", "scheduler:UpdateSchedule"], "Resource": f"arn:aws:scheduler:{acct.region}:{acct.account_id}:schedule/{aws.SCHEDULE_GROUP}/*"},
        ],
    }


def deploy(acct: aws.Account, plan: dict, every_minutes: int = 5) -> str:
    """Upload the plan and create or update the tick Lambda and its EventBridge schedule (enabled). Returns the ARN."""
    aws.setup(acct)
    acct.client("s3").put_object(Bucket=acct.bucket, Key=PLAN_KEY, Body=json.dumps({**plan, "enabled": True}, indent=2).encode())
    iam, lam = acct.client("iam"), acct.client("lambda")
    role = aws._ensure_role(iam, TICK_NAME, "lambda.amazonaws.com", _tick_policy(acct))
    code = lambda_zip()
    # up to 15 min: every failed Spot attempt takes up to a minute; reserved concurrency 1 keeps ticks from overlapping
    fn_config = dict(FunctionName=TICK_NAME, Runtime="python3.12", Handler="flowcast_model.launcher.tick.handler", Role=role, Timeout=900, MemorySize=256)
    try:
        lam.get_function(FunctionName=TICK_NAME)
        lam.update_function_code(FunctionName=TICK_NAME, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=TICK_NAME)
        lam.update_function_configuration(**{k: v for k, v in fn_config.items() if k != "FunctionName"}, FunctionName=TICK_NAME)
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):  # a new role takes a few seconds before Lambda can assume it
            try:
                lam.create_function(**fn_config, Code={"ZipFile": code}, Tags=aws.TAGS, Description="flowcast training Spot watcher tick (flowcast-train tick-deploy)")
                break
            except lam.exceptions.InvalidParameterValueException:
                if attempt == 9:
                    raise
                time.sleep(6)
    lam.get_waiter("function_active_v2").wait(FunctionName=TICK_NAME)
    arn = lam.get_function(FunctionName=TICK_NAME)["Configuration"]["FunctionArn"]
    try:
        lam.put_function_concurrency(FunctionName=TICK_NAME, ReservedConcurrentExecutions=1)
    except ClientError as err:  # small accounts can't reserve concurrency; the 5-min rate and no retries keep ticks apart
        log.warning("could not reserve concurrency 1: %s", err.response["Error"]["Code"])
    # a throttled or failed tick is dropped, never retried or queued behind a running one
    lam.put_function_event_invoke_config(FunctionName=TICK_NAME, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=60)
    invoke = aws._ensure_role(iam, INVOKE_ROLE, "scheduler.amazonaws.com", {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": arn}]})
    params = dict(
        Name=TICK_NAME,
        GroupName=aws.SCHEDULE_GROUP,
        ScheduleExpression=f"rate({every_minutes} minutes)",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={"Arn": arn, "RoleArn": invoke, "Input": "{}", "RetryPolicy": {"MaximumRetryAttempts": 0, "MaximumEventAgeInSeconds": 60}},
        State="ENABLED",
        Description="flowcast training Spot watcher: relaunch reclaimed runs, start gated runs; disables itself when done",
    )
    scheduler = acct.client("scheduler")
    for attempt in range(10):
        try:
            try:
                scheduler.create_schedule(**params)
            except scheduler.exceptions.ConflictException:
                scheduler.update_schedule(**params)
            break
        except scheduler.exceptions.ValidationException:  # the invoke role may not be assumable yet
            if attempt == 9:
                raise
            time.sleep(6)
    return arn
