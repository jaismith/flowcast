import base64
import json
import subprocess

import boto3
import pytest
from moto import mock_aws

from flowcast_model.launcher import aws


def rendered() -> str:
    return aws._render_user_data({"RUN_ID": "t-1", "BUCKET": "b", "REGION": "us-west-2", "CODE_URI": "s3://b/code/x.tar.gz", "DATASET_URIS": "s3://b/d/cube.zarr", "DEADLINE_EPOCH": 1, "MAX_BOOTS": 8, "REQUIRE_GPU": 1, "S3_REGION": "us-west-2", "DATASET_REGIONS": "us-west-2"})


def test_user_data_is_valid_bash(tmp_path):
    script = rendered()
    assert 'RUN_ID="t-1"' in script
    assert len(script.encode()) < 16000
    (tmp_path / "bootstrap.sh").write_text(script)
    subprocess.run(["bash", "-n", str(tmp_path / "bootstrap.sh")], check=True)
    job = script.split("<<'FLOWCAST_JOB'\n", 1)[1].split("\nFLOWCAST_JOB\n", 1)[0]
    (tmp_path / "job.sh").write_text(job)
    subprocess.run(["bash", "-n", str(tmp_path / "job.sh")], check=True)


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
    launched = aws.launch(other, [aws.RunSpec("far-0926", {"experiment_name": "a"}, {})], ["s3://cube-bucket/cube.zarr"], tmp_path, instance_type="c7i.4xlarge", max_hours=1)
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
