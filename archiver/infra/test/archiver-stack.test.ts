import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";

import { ArchiverStack } from "../lib/archiver-stack";

function synth(props = {}): Template {
  // Skip the uv bundling step; the template doesn't depend on the bundle contents.
  const app = new cdk.App({ context: { "aws:cdk:bundling-stacks": [] } });
  const stack = new ArchiverStack(app, "flowcast-archiver", {
    env: { account: "123456789012", region: "us-west-2" },
    ...props,
  });
  cdk.Tags.of(app).add("project", "flowcast");
  cdk.Tags.of(app).add("component", "archiver");
  return Template.fromStack(stack);
}

const TAG_LIST = [
  { Key: "component", Value: "archiver" },
  { Key: "project", Value: "flowcast" },
];
const TAGS = Match.arrayWith(TAG_LIST);

test("arm64 Python Lambda with the pandas layer, writing to the archive bucket", () => {
  const t = synth();
  t.hasResourceProperties("AWS::Lambda::Function", {
    FunctionName: "flowcast-archiver",
    Architectures: ["arm64"],
    Runtime: "python3.12",
    Handler: "flowcast_archiver.lambda_handler.handler",
    ReservedConcurrentExecutions: 1,
    Tags: TAGS,
  });
  const [fn] = Object.values(t.findResources("AWS::Lambda::Function"));
  expect(JSON.stringify(fn.Properties.Layers)).toContain(":336392948345:layer:AWSSDKPandas-Python312-Arm64:31");
  expect(JSON.stringify(fn.Properties.Environment.Variables.ARCHIVE_URI)).toMatch(/"s3:\/\/".*"\/baselines"/);
  t.hasResourceProperties("AWS::Lambda::EventInvokeConfig", { MaximumRetryAttempts: 0 });
});

test("bucket is private, versioned, retained, and tiers raw payloads", () => {
  const t = synth();
  t.hasResource("AWS::S3::Bucket", {
    DeletionPolicy: "Retain",
    Properties: Match.objectLike({
      BucketName: "flowcast-archiver-123456789012-us-west-2",
      VersioningConfiguration: { Status: "Enabled" },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true,
      },
      LifecycleConfiguration: {
        Rules: Match.arrayWith([
          Match.objectLike({
            Prefix: "baselines/raw/",
            Transitions: [{ StorageClass: "GLACIER_IR", TransitionInDays: 90 }],
          }),
        ]),
      },
      Tags: TAGS,
    }),
  });
});

test("hourly schedule, 14-day logs, and an error alarm wired to SNS", () => {
  const t = synth();
  t.hasResourceProperties("AWS::Scheduler::Schedule", {
    Name: "flowcast-archiver-hourly",
    ScheduleExpression: "cron(20 * * * ? *)",
    State: "ENABLED",
    Target: Match.objectLike({ RetryPolicy: Match.objectLike({ MaximumRetryAttempts: 0 }) }),
  });
  t.hasResourceProperties("AWS::Logs::LogGroup", {
    LogGroupName: "/aws/lambda/flowcast-archiver",
    RetentionInDays: 14,
  });
  t.hasResourceProperties("AWS::CloudWatch::Alarm", {
    AlarmName: "flowcast-archiver-errors",
    MetricName: "Errors",
    Namespace: "AWS/Lambda",
    Period: 3600,
    Threshold: 1,
    AlarmActions: [Match.objectLike({ Ref: Match.stringLikeRegexp("^Alerts") })],
  });
  t.resourceCountIs("AWS::SNS::Subscription", 0);
});

test("reads the api.data.gov key from SSM at runtime, never from the template", () => {
  const t = synth();
  const [fn] = Object.values(t.findResources("AWS::Lambda::Function"));
  expect(fn.Properties.Environment.Variables.API_DATA_GOV_KEY_PARAMETER).toBe("/flowcast/api-data-gov-key");
  expect(fn.Properties.Environment.Variables).not.toHaveProperty("API_DATA_GOV_KEY");
  const statements = Object.values(t.findResources("AWS::IAM::Policy")).flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  const [read] = statements.filter((s: any) => s.Action === "ssm:GetParameter");
  expect(JSON.stringify(read.Resource)).toContain(":parameter/flowcast/api-data-gov-key");
  const [decrypt] = statements.filter((s: any) => s.Action === "kms:Decrypt");
  expect(decrypt.Condition.StringEquals["kms:ViaService"]).toBe("ssm.us-west-2.amazonaws.com");
});

test("alert email is opt-in", () => {
  synth({ alertEmail: "someone@example.com" }).hasResourceProperties("AWS::SNS::Subscription", {
    Protocol: "email",
    Endpoint: "someone@example.com",
  });
});

test("every taggable resource carries the project and component tags", () => {
  const t = synth();
  for (const type of ["AWS::S3::Bucket", "AWS::Lambda::Function", "AWS::Logs::LogGroup", "AWS::SNS::Topic", "AWS::IAM::Role"]) {
    const resources = t.findResources(type);
    expect(Object.keys(resources).length).toBeGreaterThan(0);
    for (const resource of Object.values(resources)) {
      expect(resource.Properties.Tags).toEqual(expect.arrayContaining(TAG_LIST));
    }
  }
});
