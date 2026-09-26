import base64
import json
import subprocess

import boto3
import pytest
from moto import mock_aws

from flowcast_model.launcher import aws


def rendered() -> str:
    return aws._render_user_data({"RUN_ID": "t-1", "BUCKET": "b", "REGION": "us-west-2", "CODE_URI": "s3://b/code/x.tar.gz", "DATASET_URIS": "s3://b/d/cube.zarr", "DEADLINE_EPOCH": 1, "MAX_BOOTS": 8})


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
        monkeypatch.setattr(aws, "resolve_ami", lambda acct: image)
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
    runs = [aws.RunSpec("smoke-a-0926", {"experiment_name": "a"}, {}), aws.RunSpec("smoke-b-0926", {"experiment_name": "b"}, {"hidden_size": 256})]
    launched = aws.launch(acct, runs, ["s3://x/cube.zarr"], tmp_path, instance_type="g5.2xlarge", max_hours=1.5, sweep="smoke")
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
    assert aws.kill(acct, ["smoke-a-0926"]) == [launched[0]["instance_id"]]
