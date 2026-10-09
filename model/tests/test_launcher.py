import base64
import fnmatch
import io
import json
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from importlib import resources

import boto3
import pytest
from moto import mock_aws

from flowcast_model.launcher import aws, tick


def rendered() -> str:
    return aws._render_user_data({"RUN_ID": "t-1", "BUCKET": "b", "REGION": "us-east-2", "CODE_URI": "s3://b/code/x.tar.gz", "DATASET_URIS": "s3://b/d/cube.zarr", "DEADLINE_EPOCH": 1, "MAX_BOOTS": 8, "REQUIRE_GPU": 1})


def embedded(tag: str) -> str:
    return rendered().split(f"<<'{tag}'\n", 1)[1].split(f"\n{tag}\n", 1)[0]


def test_user_data_is_valid_bash(tmp_path):
    script = rendered()
    assert 'RUN_ID="t-1"' in script
    assert len(script.encode()) < 16000
    (tmp_path / "bootstrap.sh").write_text(script)
    subprocess.run(["bash", "-n", str(tmp_path / "bootstrap.sh")], check=True)
    for tag in ("FLOWCAST_LIB", "FLOWCAST_JOB", "FLOWCAST_GUARD"):
        (tmp_path / f"{tag}.sh").write_text(embedded(tag))
        subprocess.run(["bash", "-n", str(tmp_path / f"{tag}.sh")], check=True)
    assert "OOMPolicy=continue" in embedded("FLOWCAST_UNIT")
    assert "OnUnitActiveSec=2min" in embedded("FLOWCAST_GUARD_TIMER")


OOM_LINE = "2026-09-27T18:26:03+0000 ip-1 kernel: Out of memory: Killed process 3142 (pt_data_worker) total-vm:14473924kB"


def run_guard(tmp_path, state: str, job: str, restarts: int = 0, crash: str | None = None, stale_log: bool = False, oom: bool = True) -> dict:
    """Run the rendered guard.sh against a fake /opt/flowcast, with systemctl, aws, curl, journalctl and sleep stubbed."""
    home, bin_ = tmp_path / "flowcast", tmp_path / "bin"
    (home / "runs" / "t-1").mkdir(parents=True)
    bin_.mkdir()
    calls = tmp_path / "calls"
    stubs = {
        "systemctl": f'echo "systemctl $*" >> {calls}\ncase "$1" in is-active) echo "{job}" ;; is-system-running) echo running ;; esac',
        "aws": f'echo "aws $*" >> {calls}\ncase "$*" in *describe-instances*) echo sir-1 ;; esac',
        "curl": 'case "$*" in *spot/instance-action*) exit 22 ;; *instance-id*) echo i-1 ;; *) echo token ;; esac',
        "journalctl": f"echo '{OOM_LINE}'" if oom else "true",
        "sleep": "true",
    }
    for name, body in stubs.items():
        (bin_ / name).write_text(f"#!/bin/bash\n{body}\n")
        (bin_ / name).chmod(0o755)
    for tag, name in (("FLOWCAST_LIB", "lib.sh"), ("FLOWCAST_GUARD", "guard.sh")):
        (home / name).write_text(embedded(tag).replace("/opt/flowcast", str(home)))
    (home / "env").write_text(embedded("FLOWCAST_ENV").replace("/opt/flowcast", str(home)))
    (home / "status.json").write_text(json.dumps({"status": state, "detail": "boot 1", "boot": 1}))
    (home / "boots").write_text("1")
    (home / "heartbeat.jsonl").write_text("{}\n")
    (home / "job.log").write_text("epoch 3\n")
    if stale_log:
        subprocess.run(["touch", "-d", "30 minutes ago", str(home / "job.log")], check=True)
    if restarts:
        (home / "guard_restarts").write_text(str(restarts))
    if crash:
        (home / "crash_reason").write_text(crash)
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path)}
    subprocess.run(["bash", str(home / "guard.sh")], env=env, check=True, timeout=30)
    return {
        "status": json.loads((home / "status.json").read_text()),
        "restarts": int((home / "guard_restarts").read_text()) if (home / "guard_restarts").exists() else 0,
        "calls": calls.read_text() if calls.exists() else "",
        "log": (home / "job.log").read_text(),
    }


def test_guard_restarts_a_dead_job_once(tmp_path):
    r = run_guard(tmp_path, "training", job="failed", crash="training exited with 1")
    assert r["restarts"] == 1
    assert "systemctl stop flowcast-train.service" in r["calls"] and "systemctl start --no-block flowcast-train.service" in r["calls"]
    assert "terminate-instances" not in r["calls"] and r["status"]["status"] == "training"
    assert "training exited with 1" in r["log"] and "Killed process 3142 (pt_data_worker)" in r["log"]


def test_guard_fails_the_run_with_the_oom_reason_after_its_restart(tmp_path):
    r = run_guard(tmp_path, "training", job="inactive", restarts=1)
    assert r["status"]["status"] == "failed"
    detail = r["status"]["detail"]
    assert "job stopped during training" in detail and "2026-09-27T18:26:03 Out of memory: Killed process 3142 (pt_data_worker)" in detail
    assert "cancel-spot-instance-requests --spot-instance-request-ids sir-1" in r["calls"]
    assert "terminate-instances --instance-ids i-1" in r["calls"]
    assert "systemctl start" not in r["calls"]


def test_guard_restarts_a_stalled_training_step(tmp_path):
    r = run_guard(tmp_path, "training", job="active", stale_log=True, oom=False)
    assert r["restarts"] == 1
    assert "no log output or checkpoint for 20 min during training" in r["log"]


@pytest.mark.parametrize("state, job, stale", [("training", "active", False), ("staging", "active", True), ("done", "inactive", False), ("failed", "inactive", False)])
def test_guard_leaves_healthy_or_finished_runs_alone(tmp_path, state, job, stale):
    r = run_guard(tmp_path, state, job=job, stale_log=stale)
    assert r["restarts"] == 0 and r["status"]["status"] == state
    assert "systemctl stop" not in r["calls"] and "terminate-instances" not in r["calls"]


def test_resumed_run_drops_an_old_stop_file():
    job = rendered().split("<<'FLOWCAST_JOB'\n", 1)[1]
    restore = job.index('aws s3 sync "$RUN_S3/run/" "$RUN_DIR"')
    assert "--exclude STOP" in job[restore:].splitlines()[0]
    assert restore < job.index('rm -f "$RUN_DIR/STOP"') < job.index("flowcast-model train")


@pytest.fixture
def acct(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", aws.TRAINING_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MOTO_IAM_LOAD_MANAGED_POLICIES", "true")
    monkeypatch.setattr(aws.time, "sleep", lambda s: None)
    with mock_aws():
        image = boto3.client("ec2", region_name="us-east-2").describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
        monkeypatch.setattr(aws, "resolve_ami", lambda acct, instance_type: image)
        yield aws.Account(boto3.Session(region_name="us-east-2"))


def _cube_bucket():
    s3 = boto3.client("s3", region_name="us-east-2")
    s3.create_bucket(Bucket="cube-x", CreateBucketConfiguration={"LocationConstraint": "us-east-2"})
    s3.put_object(Bucket="cube-x", Key="cube.zarr/zarr.json", Body=b"{}")


def test_setup_is_idempotent_and_tagged(acct):
    first = aws.setup(acct)
    second = aws.setup(acct)
    assert first == second
    tags = boto3.client("s3", region_name="us-east-2").get_bucket_tagging(Bucket=acct.bucket)["TagSet"]
    assert {"Key": "component", "Value": "training"} in tags
    policy = boto3.client("iam").get_role_policy(RoleName=aws.INSTANCE_ROLE, PolicyName=aws.INSTANCE_ROLE)["PolicyDocument"]
    terminate = next(s for s in policy["Statement"] if "ec2:TerminateInstances" in s["Action"])
    assert terminate["Condition"]["StringEquals"]["aws:ResourceTag/component"] == "training"


def test_launch_sweep_tags_spot_and_reaper(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    _cube_bucket()
    runs = [aws.RunSpec("smoke-a-0926", {"experiment_name": "a"}, {}), aws.RunSpec("smoke-b-0926", {"experiment_name": "b"}, {"hidden_size": 256})]
    launched = aws.launch(acct, runs, ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.2xlarge", max_hours=1.5, sweep="smoke")
    assert len(launched) == 2
    ec2 = boto3.client("ec2")
    instances = aws.training_instances(acct)
    assert len(instances) == 2
    for inst in instances:
        tags = {t["Key"]: t["Value"] for t in inst["Tags"]}
        assert tags["project"] == "flowcast" and tags["component"] == "training" and tags["flowcast:sweep"] == "smoke"
        data = ec2.describe_instance_attribute(InstanceId=inst["InstanceId"], Attribute="userData")["UserData"]["Value"]
        assert "DEADLINE_EPOCH" in base64.b64decode(data).decode()
    schedules = boto3.client("scheduler").list_schedules(GroupName=aws.SCHEDULE_GROUP)["Schedules"]
    assert {s["Name"] for s in schedules} >= {"smoke-a-0926-terminate", "smoke-b-0926-terminate"}
    manifest = json.loads(boto3.client("s3").get_object(Bucket=acct.bucket, Key="runs/smoke-b-0926/run.json")["Body"].read())
    assert manifest["overrides"] == {"hidden_size": 256}
    assert manifest["ebs_gb"] == aws.EBS_GB
    assert aws.kill(acct, ["smoke-a-0926"]) == [launched[0]["instance_id"]]
    status = json.loads(boto3.client("s3").get_object(Bucket=acct.bucket, Key="runs/smoke-a-0926/status.json")["Body"].read())
    assert status["status"] == "killed" and status["instance"] == launched[0]["instance_id"]


def test_on_demand_launch_has_no_spot_request_and_keeps_the_reaper(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    _cube_bucket()
    launched = aws.launch(acct, [aws.RunSpec("od-0928", {"experiment_name": "a"}, {})], ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge", max_hours=2, on_demand=True)
    assert launched[0]["market"] == "on-demand" and launched[0]["spot_request_id"] is None
    inst = aws.training_instances(acct)[0]
    assert "InstanceLifecycle" not in inst and "SpotInstanceRequestId" not in inst
    shutdown = boto3.client("ec2").describe_instance_attribute(InstanceId=inst["InstanceId"], Attribute="instanceInitiatedShutdownBehavior")
    assert shutdown["InstanceInitiatedShutdownBehavior"]["Value"] == "terminate"
    schedules = {s["Name"] for s in boto3.client("scheduler").list_schedules(GroupName=aws.SCHEDULE_GROUP)["Schedules"]}
    assert "od-0928-terminate" in schedules and "od-0928-cancel" not in schedules
    assert aws.run_cost(acct, "od-0928")["spot_usd_per_h"] == aws.ON_DEMAND_USD_PER_H["g5.xlarge"]


def test_on_demand_quota_errors_count_as_no_capacity(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    _cube_bucket()
    real = aws.Account.client

    def client(self, name, region=None):
        c = real(self, name, region)
        if name == "ec2":
            def no_quota(**kwargs):
                raise aws.ClientError({"Error": {"Code": "VcpuLimitExceeded", "Message": "quota 0"}}, "RunInstances")
            c.run_instances = no_quota
        return c

    monkeypatch.setattr(aws.Account, "client", client)
    with pytest.raises(aws.NoCapacityError, match="On-Demand"):
        aws.launch(acct, [aws.RunSpec("od-0928", {"experiment_name": "a"}, {})], ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge", max_hours=2, on_demand=True)


def test_launch_avoids_the_given_zones(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    _cube_bucket()
    first = aws.launch(acct, [aws.RunSpec("az-a-0928", {"experiment_name": "a"}, {})], ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge", max_hours=1)[0]
    second = aws.launch(acct, [aws.RunSpec("az-b-0928", {"experiment_name": "b"}, {})], ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge", max_hours=1, avoid_azs=(first["availability_zone"],))[0]
    assert second["availability_zone"] != first["availability_zone"]
    zones = {s["AvailabilityZone"] for s in boto3.client("ec2").describe_subnets()["Subnets"]}
    with pytest.raises(aws.NoCapacityError):
        aws.launch(acct, [aws.RunSpec("az-c-0928", {"experiment_name": "c"}, {})], ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge", max_hours=1, avoid_azs=tuple(zones))


def test_gpu_families():
    assert aws.is_gpu("g5.2xlarge") and aws.is_gpu("g6e.xlarge") and aws.is_gpu("p4d.24xlarge")
    assert not aws.is_gpu("c7i.4xlarge") and not aws.is_gpu("m7i.2xlarge")


def test_account_is_pinned_to_the_training_region(acct):
    assert acct.region == aws.TRAINING_REGION == "us-east-2"
    assert acct.bucket == f"flowcast-training-{acct.account_id}-us-east-2"
    assert acct.legacy_bucket == f"flowcast-training-{acct.account_id}"
    assert aws.Account(boto3.Session(region_name="us-west-2")).region == "us-east-2"


def test_launch_rejects_a_dataset_outside_the_training_region(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    boto3.client("s3", region_name="us-west-2").create_bucket(Bucket="cube-far", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    with pytest.raises(ValueError, match="training reads datasets from us-east-2 only"):
        aws.launch(acct, [aws.RunSpec("far-0926", {"experiment_name": "a"}, {})], ["s3://cube-far/cube.zarr"], tmp_path, instance_type="g5.xlarge", max_hours=1)
    assert not aws.training_instances(acct)
    assert not aws._exists(boto3.client("s3"), acct.bucket, "runs/far-0926/config.yml")


def test_user_data_names_only_the_training_region(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    _cube_bucket()
    m = aws.launch(acct, [aws.RunSpec("one-0926", {"experiment_name": "a"}, {})], ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge", max_hours=1)[0]
    assert m["region"] == "us-east-2" and m["datasets"] == ["s3://cube-x/cube.zarr"]
    spec = aws.read_launch_spec(acct, "one-0926")
    assert "dataset_regions" not in spec and "publish" not in spec
    data = boto3.client("ec2").describe_instance_attribute(InstanceId=m["instance_id"], Attribute="userData")["UserData"]["Value"]
    user_data = base64.b64decode(data).decode()
    assert 'REGION="us-east-2"' in user_data
    assert "S3_REGION" not in user_data and "DATASET_REGIONS" not in user_data and "REPLICA_URIS" not in user_data


def test_bootstrap_syncs_datasets_from_its_own_region_without_replicas():
    script = resources.files("flowcast_model.launcher").joinpath("bootstrap.sh").read_text()
    assert 'aws s3 sync "$uri" "$dest" --region "$REGION"' in script
    assert "publish_replica" not in script and "DATASET_REGIONS" not in script and "_replicas/" not in script
    assert 'AWS_DEFAULT_REGION="$REGION"' in script


def test_gpu_slots_fit_the_free_quota_cheapest_first(acct, monkeypatch):
    quota = {"v": 8.0}
    monkeypatch.setattr(aws, "spot_quota_vcpus", lambda a: quota["v"])
    monkeypatch.setattr(aws, "running_gpu_vcpus", lambda a: 0)
    monkeypatch.setattr(aws, "instance_vcpus", lambda a, t: 8 if "2xlarge" in t else 4)
    monkeypatch.setattr(aws, "spot_price", lambda a, t: {"g6.xlarge": 0.39, "g4dn.xlarge": 0.20, "g5.2xlarge": 0.55}.get(t))
    assert aws.plan_gpu_slots(acct, 5, ("g6.xlarge", "g4dn.xlarge", "g5.2xlarge")) == ["g4dn.xlarge", "g4dn.xlarge"]
    assert aws.plan_gpu_slots(acct, 5, ("g6.xlarge", "g4dn.xlarge", "g5.2xlarge"), by_price=False) == ["g6.xlarge", "g6.xlarge"]
    quota["v"] = 0.0
    assert aws.plan_gpu_slots(acct, 5) == []


def test_fp32_runs_prefer_tf32_gpus_with_g4dn_last():
    assert aws.trains_in_fp32({"flowcast": {"train": {"amp": "none"}}}) and aws.trains_in_fp32({})
    assert not aws.trains_in_fp32({"flowcast": {"train": {"amp": "bf16"}}})
    types = [["g6.xlarge", 9, 0.7], ["g4dn.xlarge", 9, 0.4], ["g5.xlarge", 9, 0.8], ["g6.2xlarge", 9, 0.9], ["x1.big", 9, 1.0]]
    assert [t[0] for t in aws.fp32_order(types)] == ["g6.xlarge", "g5.xlarge", "g6.2xlarge", "g4dn.xlarge", "x1.big"]


def test_tick_tries_fp32_types_for_a_run_that_fell_back(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["fb-0928", "bf-0928"])
    s3 = boto3.client("s3")
    for rid in ("fb-0928", "bf-0928"):
        s3.put_object(Bucket=acct.bucket, Key=f"runs/{rid}/config.yml", Body=b"flowcast: {train: {amp: bf16}}\n")
    s3.put_object(Bucket=acct.bucket, Key="runs/fb-0928/run/amp_fallback.json", Body=b'{"epoch": 50}')
    tried = []

    def relaunch(acct_, rid, itype, *a, **k):
        tried.append((rid, itype))
        raise aws.NoCapacityError("none")

    monkeypatch.setattr(aws, "relaunch", relaunch)
    types = [["g6.xlarge", 2, 0.7], ["g4dn.xlarge", 2, 0.4], ["g5.xlarge", 2, 0.8]]
    _plan(acct, [{"run_id": "fb-0928", "types": types}, {"run_id": "bf-0928", "types": types}])
    tick.tick(acct)
    assert [t for r, t in tried if r == "fb-0928"] == ["g6.xlarge", "g5.xlarge", "g4dn.xlarge"]
    assert [t for r, t in tried if r == "bf-0928"] == ["g6.xlarge", "g4dn.xlarge", "g5.xlarge"]


def test_data_placement_uses_nvme_only_when_the_cube_fits(acct, monkeypatch):
    monkeypatch.setattr(aws, "s3_prefix_bytes", lambda acct_, uri: 128e9)
    monkeypatch.setattr(aws, "instance_storage_gb", lambda acct_, itype: {"g4dn.xlarge": 125.0, "g5.xlarge": 250.0}.get(itype, 0.0))
    assert aws.data_placement(acct, ["s3://c/cube.zarr"], "g5.xlarge") == (aws.EBS_GB, False)
    ebs, on_ebs = aws.data_placement(acct, ["s3://c/cube.zarr"], "g4dn.xlarge")
    assert on_ebs and ebs == aws.EBS_GB + 141  # 128 GB x 1.1, rounded up
    assert aws.data_placement(acct, ["s3://c/cube.zarr"], "g5.xlarge", force_ebs=True) == (aws.EBS_GB + 141, True)


def test_restarted_instance_skips_a_completed_dataset_copy():
    script = resources.files("flowcast_model.launcher").joinpath("bootstrap.sh").read_text()
    skip = script.index('if [ -f "$dest.complete" ]')
    sync = script.index('aws s3 sync "$uri" "$dest"')
    mark = script.index('echo "$uri" > "$dest.complete"')
    assert skip < sync < mark


def _stage(acct, monkeypatch, tmp_path, run_ids):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    _cube_bucket()
    specs = [aws.RunSpec(r, {"experiment_name": r}, {"seed": 42}) for r in run_ids]
    return aws.launch(acct, specs, ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge", stage_only=True, data_on_ebs=True)


def _plan(acct, jobs):
    boto3.client("s3").put_object(Bucket=acct.bucket, Key=tick.PLAN_KEY, Body=json.dumps({"jobs": jobs}).encode())


def _hindcast(acct, run_id):
    boto3.client("s3").put_object(Bucket=acct.bucket, Key=f"runs/{run_id}/run/hindcast/_hindcast.json", Body=b"{}")


def test_stage_only_writes_the_launch_spec_without_an_instance(acct, monkeypatch, tmp_path):
    staged = _stage(acct, monkeypatch, tmp_path, ["st-0928"])
    assert staged[0]["data_on_ebs"] and not aws.training_instances(acct)
    assert aws.read_launch_spec(acct, "st-0928")["code"].endswith("code/abc123.tar.gz")


def test_tick_relaunches_reclaimed_runs_once_gates_and_finishes(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["tk-a-0928", "tk-b-0928"])
    _plan(acct, [{"run_id": "tk-a-0928", "types": [["g5.xlarge", 2, 0.7]]}, {"run_id": "tk-b-0928", "types": [["g5.xlarge", 2, 0.7]], "after": ["tk-a-0928"]}])
    now = datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc)  # even 5-min slot: no zone avoidance
    first = tick.tick(acct, now)
    assert first["actions"]["tk-a-0928"].startswith("launched g5.xlarge") and first["actions"]["tk-b-0928"] == "waiting for tk-a-0928"
    tick.tick(acct, now + timedelta(minutes=10))
    assert len(tick.instances(acct, "tk-a-0928")) == 1  # an existing instance is never launched twice
    ec2 = boto3.client("ec2")
    iid = tick.instances(acct, "tk-a-0928")[0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[iid])
    reclaimed = tick.tick(acct, datetime.now(timezone.utc) + timedelta(minutes=30))
    assert reclaimed["actions"]["tk-a-0928"].startswith("killed")
    again = tick.tick(acct, now + timedelta(minutes=40))
    assert again["actions"]["tk-a-0928"].startswith("launched")
    _hindcast(acct, "tk-a-0928")
    gated = tick.tick(acct, now + timedelta(minutes=50))
    assert gated["actions"]["tk-a-0928"] == "done" and gated["actions"]["tk-b-0928"].startswith("launched")
    boto3.client("scheduler").create_schedule(Name=tick.TICK_NAME, GroupName=aws.SCHEDULE_GROUP, ScheduleExpression="rate(5 minutes)", FlexibleTimeWindow={"Mode": "OFF"}, Target={"Arn": "arn:aws:lambda:us-west-2:123456789012:function:x", "RoleArn": "arn:aws:iam::123456789012:role/x"})
    _hindcast(acct, "tk-b-0928")
    final = tick.tick(acct, now + timedelta(minutes=60))
    assert final["finished"]
    assert boto3.client("scheduler").get_schedule(Name=tick.TICK_NAME, GroupName=aws.SCHEDULE_GROUP)["State"] == "DISABLED"
    assert tick.read_plan(acct)["enabled"] is False
    assert tick.tick(acct, now + timedelta(minutes=65))["enabled"] is False


def test_tick_starts_no_launch_after_its_time_budget(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["tk-t-0928"])
    _plan(acct, [{"run_id": "tk-t-0928", "types": [["g5.xlarge", 2, 0.7]]}])
    assert "time budget" in tick.tick(acct, budget_s=-1)["actions"]["tk-t-0928"] and not aws.training_instances(acct)


def test_tick_leaves_a_failed_run_alone(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["tk-f-0928"])
    _plan(acct, [{"run_id": "tk-f-0928", "types": [["g5.xlarge", 2, 0.7]]}])
    boto3.client("s3").put_object(Bucket=acct.bucket, Key="runs/tk-f-0928/status.json", Body=json.dumps({"status": "failed"}).encode())
    assert tick.tick(acct)["actions"]["tk-f-0928"].startswith("failed") and not aws.training_instances(acct)


def test_launch_refuses_an_empty_or_missing_dataset(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["ok-0928"])
    s3 = boto3.client("s3", region_name="us-east-2")
    s3.put_object(Bucket="cube-x", Key="other.zarr/c/0", Body=b"x")
    for uri in ("s3://cube-x/gone.zarr", "s3://cube-x/other.zarr", "s3://cube-x/nothing"):
        with pytest.raises(aws.EmptyDatasetError):
            aws.launch(acct, [aws.RunSpec("em-0928", {"experiment_name": "em"}, {})], [uri], tmp_path, instance_type="g5.xlarge", stage_only=True)


def test_tick_marks_a_run_whose_dataset_vanished(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["dv-0928"])
    boto3.client("s3").delete_object(Bucket="cube-x", Key="cube.zarr/zarr.json")
    _plan(acct, [{"run_id": "dv-0928", "types": [["g5.xlarge", 2, 0.7]]}])
    assert tick.tick(acct)["actions"]["dv-0928"].startswith("dataset missing or empty") and not aws.training_instances(acct)


def test_tick_disables_itself_after_repeated_failures(acct, monkeypatch, tmp_path):
    runs = ["rf-a-0928", "rf-b-0928", "rf-c-0928"]
    _stage(acct, monkeypatch, tmp_path, runs)
    _plan(acct, [{"run_id": r, "types": [["g5.xlarge", 2, 0.7]]} for r in runs])
    boto3.client("scheduler").create_schedule(Name=tick.TICK_NAME, GroupName=aws.SCHEDULE_GROUP, ScheduleExpression="rate(5 minutes)", FlexibleTimeWindow={"Mode": "OFF"}, Target={"Arn": "arn:aws:lambda:us-east-2:123456789012:function:x", "RoleArn": "arn:aws:iam::123456789012:role/x"})
    s3 = boto3.client("s3")
    s3.put_object(Bucket=acct.bucket, Key="runs/rf-b-0928/status.json", Body=json.dumps({"status": "failed"}).encode())
    one = tick.tick(acct)
    assert not one.get("finished") and one["actions"]["rf-a-0928"].startswith("launched")
    aws.kill(acct, ["rf-a-0928", "rf-c-0928"])
    s3.put_object(Bucket=acct.bucket, Key="runs/rf-c-0928/status.json", Body=json.dumps({"status": "failed"}).encode())
    s3.put_object(Bucket=acct.bucket, Key="runs/rf-a-0928/status.json", Body=json.dumps({"status": "training"}).encode())
    two = tick.tick(acct)
    assert two["finished"] and "2 runs failed" in two["stopped"]
    assert two["actions"]["rf-a-0928"] == "not launched: too many failed runs" and not aws.training_instances(acct)
    assert tick.read_plan(acct)["enabled"] is False
    assert boto3.client("scheduler").get_schedule(Name=tick.TICK_NAME, GroupName=aws.SCHEDULE_GROUP)["State"] == "DISABLED"


def test_lambda_zip_has_the_launcher_and_yaml():
    names = set(zipfile.ZipFile(io.BytesIO(tick.lambda_zip())).namelist())
    assert {"flowcast_model/__init__.py", "flowcast_model/launcher/aws.py", "flowcast_model/launcher/tick.py", "flowcast_model/launcher/bootstrap.sh", "yaml/__init__.py"} <= names


def test_deploy_creates_the_lambda_and_an_enabled_schedule(acct):
    arn = tick.deploy(acct, {"jobs": []})
    assert arn.endswith(f"function:{tick.TICK_NAME}")
    schedule = boto3.client("scheduler").get_schedule(Name=tick.TICK_NAME, GroupName=aws.SCHEDULE_GROUP)
    assert schedule["State"] == "ENABLED" and schedule["ScheduleExpression"] == "rate(5 minutes)"
    assert tick.read_plan(acct)["enabled"] is True
    fn = boto3.client("lambda").get_function(FunctionName=tick.TICK_NAME)["Configuration"]
    assert fn["Timeout"] == 900


def test_failed_relaunch_keeps_an_existing_runs_config(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["kc-0928"])
    monkeypatch.setattr(aws, "_start", lambda *a, **k: (_ for _ in ()).throw(aws.NoCapacityError("none")))
    with pytest.raises(aws.NoCapacityError):
        aws.launch(acct, [aws.RunSpec("kc-0928", {"experiment_name": "kc"}, {})], ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge")
    assert aws._exists(boto3.client("s3"), acct.bucket, "runs/kc-0928/config.yml")
    with pytest.raises(aws.NoCapacityError):
        aws.launch(acct, [aws.RunSpec("new-0928", {"experiment_name": "new"}, {})], ["s3://cube-x/cube.zarr"], tmp_path, instance_type="g5.xlarge")
    assert not aws._exists(boto3.client("s3"), acct.bucket, "runs/new-0928/config.yml")


def test_tick_never_launches_a_run_without_its_config(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["mc-0928"])
    boto3.client("s3").delete_object(Bucket=acct.bucket, Key="runs/mc-0928/config.yml")
    _plan(acct, [{"run_id": "mc-0928", "types": [["g5.xlarge", 2, 0.7]]}])
    assert tick.tick(acct)["actions"]["mc-0928"] == "config.yml missing: needs a person"
    assert not aws.training_instances(acct)


def test_replacement_instance_restores_finished_hindcast_sites():
    script = resources.files("flowcast_model.launcher").joinpath("bootstrap.sh").read_text()
    restore = next(line for line in script.splitlines() if 'aws s3 sync "$RUN_S3/run/" "$RUN_DIR"' in line)
    assert "hindcast" not in restore and "--exclude STOP" in restore


def test_tick_upgrades_a_slow_instance_without_a_gap(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["up-0928"])
    _plan(acct, [{"run_id": "up-0928", "types": [["g4dn.xlarge", 2, 0.4]]}])
    now = datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc)
    assert tick.tick(acct, now)["actions"]["up-0928"].startswith("launched g4dn.xlarge")
    old = tick.instances(acct, "up-0928")[0]["InstanceId"]
    _plan(acct, [{"run_id": "up-0928", "types": [["g4dn.xlarge", 2, 0.4]], "upgrade": {"from": ["g4dn.xlarge"], "to": [["g5.xlarge", 2, 0.7]]}}])
    boto3.client("s3").put_object(Bucket=acct.bucket, Key="runs/up-0928/status.json", Body=json.dumps({"status": "hindcast"}).encode())
    assert tick.tick(acct, now + timedelta(minutes=10))["actions"]["up-0928"] == "running"  # never moves a hindcast
    boto3.client("s3").put_object(Bucket=acct.bucket, Key="runs/up-0928/status.json", Body=json.dumps({"status": "training"}).encode())
    moved = tick.tick(acct, now + timedelta(minutes=20))
    assert moved["actions"]["up-0928"].startswith("upgraded g4dn.xlarge -> g5.xlarge")
    live = [i for i in tick.instances(acct, "up-0928") if i["State"]["Name"] in ("pending", "running")]
    assert [i["InstanceType"] for i in live] == ["g5.xlarge"] and old not in {i["InstanceId"] for i in live}


def test_dataset_volumes_get_fast_throughput_and_the_sync_runs_in_parallel():
    assert aws.root_volume(250, True)["Throughput"] == 1000 and aws.root_volume(250, True)["Iops"] == 8000
    assert "Throughput" not in aws.root_volume(100, False)
    script = resources.files("flowcast_model.launcher").joinpath("bootstrap.sh").read_text()
    assert script.index("max_concurrent_requests 64") < script.index('aws s3 sync "$uri" "$dest"')


def test_tick_role_may_pass_its_own_invoke_role(acct):
    passable = next(st for st in tick._tick_policy(acct)["Statement"] if st["Action"] == ["iam:PassRole"])["Resource"]
    assert any(r.endswith(f"role/{tick.INVOKE_ROLE}") for r in passable)


def test_tick_role_is_scoped_to_the_training_region(acct):
    statements = tick._tick_policy(acct)["Statement"]
    scheduler = next(st for st in statements if "scheduler:CreateSchedule" in st["Action"])["Resource"]
    listable = next(st for st in statements if "s3:ListBucket" in st["Action"])["Resource"]
    assert fnmatch.fnmatch(f"arn:aws:scheduler:us-east-2:{acct.account_id}:schedule/{aws.SCHEDULE_GROUP}/r-0928-terminate", scheduler)
    assert not fnmatch.fnmatch(f"arn:aws:scheduler:us-west-2:{acct.account_id}:schedule/{aws.SCHEDULE_GROUP}/r-0928-terminate", scheduler)
    assert listable == [f"arn:aws:s3:::{acct.bucket}"]


def test_tick_never_launches_runs_trained_off_aws(acct, monkeypatch, tmp_path):
    _stage(acct, monkeypatch, tmp_path, ["full-v2c-s43-0930-pc"])
    _plan(acct, [{"run_id": "full-v2c-s43-0930-pc", "types": [["g5.xlarge", 2, 0.7]]}])
    state = tick.tick(acct)
    assert state["actions"]["full-v2c-s43-0930-pc"].startswith("external host") and not aws.training_instances(acct)
