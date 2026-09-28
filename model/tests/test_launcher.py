import base64
import json
import subprocess

import boto3
import pytest
from moto import mock_aws

from flowcast_model.launcher import aws


def rendered() -> str:
    return aws._render_user_data({"RUN_ID": "t-1", "BUCKET": "b", "REGION": "us-west-2", "CODE_URI": "s3://b/code/x.tar.gz", "DATASET_URIS": "s3://b/d/cube.zarr", "DEADLINE_EPOCH": 1, "MAX_BOOTS": 8, "REQUIRE_GPU": 1, "S3_REGION": "us-west-2", "DATASET_REGIONS": "us-west-2"})


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
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("MOTO_IAM_LOAD_MANAGED_POLICIES", "true")
    monkeypatch.setattr(aws.time, "sleep", lambda s: None)
    with mock_aws():
        image = boto3.client("ec2", region_name="us-west-2").describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
        monkeypatch.setattr(aws, "resolve_ami", lambda acct, instance_type: image)
        yield aws.Account(boto3.Session(region_name="us-west-2"))


def test_setup_is_idempotent_and_tagged(acct):
    first = aws.setup(acct)
    second = aws.setup(acct)
    assert first == second
    tags = boto3.client("s3", region_name="us-west-2").get_bucket_tagging(Bucket=acct.bucket)["TagSet"]
    assert {"Key": "component", "Value": "training"} in tags
    policy = boto3.client("iam").get_role_policy(RoleName=aws.INSTANCE_ROLE, PolicyName=aws.INSTANCE_ROLE)["PolicyDocument"]
    terminate = next(s for s in policy["Statement"] if "ec2:TerminateInstances" in s["Action"])
    assert terminate["Condition"]["StringEquals"]["aws:ResourceTag/component"] == "training"


def test_launch_sweep_tags_spot_and_reaper(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    boto3.client("s3", region_name="us-west-2").create_bucket(Bucket="cube-x", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
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
    boto3.client("s3", region_name="us-west-2").create_bucket(Bucket="cube-x", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
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
    boto3.client("s3", region_name="us-west-2").create_bucket(Bucket="cube-x", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
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


def test_gpu_families():
    assert aws.is_gpu("g5.2xlarge") and aws.is_gpu("g6e.xlarge") and aws.is_gpu("p4d.24xlarge")
    assert not aws.is_gpu("c7i.4xlarge") and not aws.is_gpu("m7i.2xlarge")


def test_launch_in_another_region_keeps_home_bucket(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.create_bucket(Bucket="cube-bucket", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    s3.put_object(Bucket="cube-bucket", Key="cube.zarr/q/c/0", Body=b"x" * 2_000_000)
    s3.put_object(Bucket="cube-bucket", Key="cube.zarr.bak/q/c/0", Body=b"x" * 5_000_000)
    other = acct.in_region("eu-west-1")
    launched = aws.launch(other, [aws.RunSpec("far-0926", {"experiment_name": "a"}, {})], ["s3://cube-bucket/cube.zarr"], tmp_path, instance_type="c7i.4xlarge", max_hours=1, replicate=False)
    assert launched[0]["region"] == "eu-west-1"
    assert launched[0]["ebs_gb"] == aws.EBS_GB + 1  # cross-region cube cached on the root volume (2 MB, rounded up)
    assert aws.training_instances(acct, regions=["eu-west-1"])[0]["InstanceId"] == launched[0]["instance_id"]
    assert aws.training_instances(acct) == []
    runs = aws.list_runs(acct)
    assert runs[0]["instance_state"] == "running"
    data = boto3.client("ec2", region_name="eu-west-1").describe_instance_attribute(InstanceId=launched[0]["instance_id"], Attribute="userData")["UserData"]["Value"]
    user_data = base64.b64decode(data).decode()
    assert 'S3_REGION="us-west-2"' in user_data and 'DATASET_REGIONS="us-west-2"' in user_data and 'REGION="eu-west-1"' in user_data
    assert aws.kill(acct, ["far-0926"], regions=["eu-west-1"]) == [launched[0]["instance_id"]]


def test_cross_region_runs_publish_and_then_read_an_in_region_replica(acct, monkeypatch, tmp_path):
    monkeypatch.setattr(aws, "package_code", lambda repo: (b"tarball", "abc123"))
    src = boto3.client("s3", region_name="us-west-2")
    src.create_bucket(Bucket="cube-bucket", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
    for k in ("a", "b"):
        src.put_object(Bucket="cube-bucket", Key=f"cube.zarr/q/{k}", Body=k.encode() * 1000)
    other = acct.in_region("eu-west-1")
    target = f"s3://{other.replica_bucket}/cube.zarr"

    def launch(run_id):
        m = aws.launch(other, [aws.RunSpec(run_id, {"experiment_name": "a"}, {})], ["s3://cube-bucket/cube.zarr"], tmp_path, instance_type="c7i.4xlarge", max_hours=1)[0]
        data = boto3.client("ec2", region_name="eu-west-1").describe_instance_attribute(InstanceId=m["instance_id"], Attribute="userData")["UserData"]["Value"]
        return m, base64.b64decode(data).decode()

    first, user_data = launch("far-a")
    assert first["datasets"] == ["s3://cube-bucket/cube.zarr"] and f'REPLICA_URIS="{target}"' in user_data

    dst = boto3.client("s3", region_name="eu-west-1")
    dst.put_object(Bucket=other.replica_bucket, Key="_replicas/cube.zarr.json", Body=json.dumps(aws.source_etags(other, "s3://cube-bucket/cube.zarr")).encode())
    second, user_data = launch("far-b")
    assert second["datasets"] == [target] and 'REPLICA_URIS="-"' in user_data and 'DATASET_REGIONS="eu-west-1"' in user_data

    src.put_object(Bucket="cube-bucket", Key="cube.zarr/q/b", Body=b"B" * 1000)
    third, user_data = launch("far-c")
    assert third["datasets"] == ["s3://cube-bucket/cube.zarr"] and f'REPLICA_URIS="{target}"' in user_data


def test_publish_replica_uploads_then_writes_the_source_etag_marker(tmp_path):
    job = embedded("FLOWCAST_JOB")
    fn = job[job.index("publish_replica() {") : job.index("\n}\n", job.index("publish_replica() {")) + 3]
    assert job.index('aws s3 sync "$uri" "$dest"') < job.index('publish_replica "$dest"')
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "aws").write_text(f'#!/bin/bash\necho "$*" >> {tmp_path}/calls\n[ "$2" = cp ] && cp "$3" {tmp_path}/marker.json\nexit 0\n')
    (bin_ / "aws").chmod(0o755)
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps([["cube.zarr/q/a", '"e1"'], ["cube.zarr/q/b", '"e2"']]))
    script = f'REGION=eu-west-1\n{fn}\npublish_replica {tmp_path}/data s3://rb/cube.zarr {listing} {tmp_path}/done\n'
    subprocess.run(["bash", "-c", script], env={"PATH": f"{bin_}:/usr/bin:/bin"}, check=True)
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0].startswith(f"s3 sync {tmp_path}/data s3://rb/cube.zarr") and "--delete" in calls[0]
    assert "s3://rb/_replicas/cube.zarr.json" in calls[1]
    assert json.loads((tmp_path / "marker.json").read_text()) == {"cube.zarr/q/a": "e1", "cube.zarr/q/b": "e2"}
    assert (tmp_path / "done").exists()


def test_pick_region_uses_quota(acct, monkeypatch):
    quotas = {"us-west-2": 0.0, "us-east-2": 32.0}
    monkeypatch.setattr(aws, "spot_quota_vcpus", lambda a, r: quotas[r])
    monkeypatch.setattr(aws, "instance_vcpus", lambda a, r, t: 8)
    monkeypatch.setattr(aws, "CANDIDATE_REGIONS", ("us-east-2",))
    assert aws.pick_region(acct, "g5.2xlarge", 2) == "us-east-2"
    quotas["us-east-2"] = 8.0
    with pytest.raises(RuntimeError):
        aws.pick_region(acct, "g5.2xlarge", 2)
    assert aws.pick_region(acct, "c7i.4xlarge", 5) == acct.region


def test_auto_instance_falls_back_to_cpu_then_prefers_gpu(acct, monkeypatch):
    quotas = {"us-west-2": 0.0, "us-east-2": 0.0}
    monkeypatch.setattr(aws, "spot_quota_vcpus", lambda a, r: quotas[r])
    monkeypatch.setattr(aws, "instance_vcpus", lambda a, r, t: 8)
    monkeypatch.setattr(aws, "CANDIDATE_REGIONS", ("us-east-2",))
    assert aws.choose_instance(acct, 3, "c8g.8xlarge") == ("c8g.8xlarge", acct.home_region)
    quotas["us-east-2"] = 32.0
    itype, region = aws.choose_instance(acct, 3, "c8g.8xlarge")
    assert region == "us-east-2" and aws.is_gpu(itype)


def test_gpu_slots_span_regions(acct, monkeypatch):
    quotas = {"us-west-2": 8.0, "us-east-2": 8.0}
    monkeypatch.setattr(aws, "spot_quota_vcpus", lambda a, r: quotas[r])
    monkeypatch.setattr(aws, "running_gpu_vcpus", lambda a, r: 0)
    monkeypatch.setattr(aws, "instance_vcpus", lambda a, r, t: 8 if "2xlarge" in t else 4)
    monkeypatch.setattr(aws, "CANDIDATE_REGIONS", ("us-east-2",))
    slots = aws.plan_gpu_slots(acct, 5, ("g5.2xlarge",))
    assert sorted(r for _, r in slots) == ["us-east-2", "us-west-2"]
    slots = aws.plan_gpu_slots(acct, 5, ("g5.xlarge",))
    assert len(slots) == 4


def test_data_placement_uses_nvme_only_when_the_cube_fits(acct, monkeypatch):
    monkeypatch.setattr(aws, "s3_prefix_bytes", lambda acct_, uri, region: 128e9)
    monkeypatch.setattr(aws, "instance_storage_gb", lambda acct_, itype: {"g4dn.xlarge": 125.0, "g5.xlarge": 250.0}.get(itype, 0.0))
    home = acct.region
    assert aws.data_placement(acct, ["s3://c/cube.zarr"], [home], "g5.xlarge") == (aws.EBS_GB, False)
    ebs, on_ebs = aws.data_placement(acct, ["s3://c/cube.zarr"], [home], "g4dn.xlarge")
    assert on_ebs and ebs == aws.EBS_GB + 141  # 128 GB x 1.1, rounded up
    assert aws.data_placement(acct, ["s3://c/cube.zarr"], ["eu-west-1"], "g5.xlarge")[1]  # cross-region: always the root volume
