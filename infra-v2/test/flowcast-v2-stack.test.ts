import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";

import { FlowcastV2Stack } from "../lib/flowcast-v2-stack";

let template: Template;

beforeAll(() => {
  const app = new cdk.App();
  const stack = new FlowcastV2Stack(app, "flowcast-v2", { env: { account: "111111111111", region: "eu-west-1" } });
  cdk.Tags.of(app).add("project", "flowcast");
  template = Template.fromStack(stack);
});

test("both functions are arm64 Python with the pandas layer, one run at a time", () => {
  for (const name of ["flowcast-obs-ingest", "flowcast-skill-page"]) {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: name,
      Architectures: ["arm64"],
      Runtime: "python3.12",
      ReservedConcurrentExecutions: 1,
      Tags: Match.arrayWith([{ Key: "project", Value: "flowcast" }]),
    });
  }
  const functions = Object.values(template.findResources("AWS::Lambda::Function"));
  expect(functions.filter((f: any) => JSON.stringify(f.Properties.Layers).includes("AWSSDKPandas-Python312-Arm64"))).toHaveLength(2);
});

test("schedules: hourly ingest, weekly revisions with a 30-day window, daily skill page", () => {
  template.hasResourceProperties("AWS::Scheduler::Schedule", { Name: "flowcast-obs-ingest-hourly", ScheduleExpression: "cron(10 * * * ? *)" });
  template.hasResourceProperties("AWS::Scheduler::Schedule", {
    Name: "flowcast-obs-ingest-revisions-weekly",
    ScheduleExpression: "cron(40 4 ? * MON *)",
    Target: Match.objectLike({ Input: JSON.stringify({ window_hours: 720 }) }),
  });
  template.hasResourceProperties("AWS::Scheduler::Schedule", { Name: "flowcast-skill-page-daily", ScheduleExpression: "cron(30 15 * * ? *)" });
});

test("alarms cover failures and missed ingest cycles", () => {
  template.resourceCountIs("AWS::CloudWatch::Alarm", 3);
  template.hasResourceProperties("AWS::CloudWatch::Alarm", {
    AlarmName: "flowcast-obs-ingest-missing",
    ComparisonOperator: "LessThanThreshold",
    TreatMissingData: "breaching",
  });
});

test("logs are kept 14 days", () => {
  template.resourcePropertiesCountIs("AWS::Logs::LogGroup", { RetentionInDays: 14 }, 2);
});

test("the skill page is private S3 behind CloudFront with origin access control", () => {
  template.hasResourceProperties("AWS::CloudFront::Distribution", {
    DistributionConfig: Match.objectLike({ DefaultRootObject: "index.html", PriceClass: "PriceClass_100" }),
  });
  template.resourceCountIs("AWS::CloudFront::OriginAccessControl", 1);
  template.allResourcesProperties("AWS::S3::Bucket", {
    PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
  });
});

test("the archiver bucket is only read", () => {
  const policies = template.findResources("AWS::IAM::Policy");
  const archiverStatements = Object.values(policies)
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement)
    .filter((s: any) => JSON.stringify(s.Resource).includes("flowcast-archiver-"));
  expect(archiverStatements.length).toBeGreaterThan(0);
  for (const s of archiverStatements) {
    const actions = [s.Action].flat();
    expect(actions.every((a: string) => a.startsWith("s3:Get") || a.startsWith("s3:List"))).toBe(true);
  }
});

test("both functions read the api.data.gov key from SSM at runtime, never from the template", () => {
  const functions = Object.values(template.findResources("AWS::Lambda::Function"));
  for (const f of functions as any[]) {
    const env = f.Properties.Environment.Variables;
    expect(env.API_DATA_GOV_KEY_PARAMETER).toBe("/flowcast/api-data-gov-key");
    expect(env).not.toHaveProperty("API_DATA_GOV_KEY");
  }
  const statements = Object.values(template.findResources("AWS::IAM::Policy")).flatMap((p: any) => p.Properties.PolicyDocument.Statement);
  const reads = statements.filter((s: any) => s.Action === "ssm:GetParameter");
  expect(reads).toHaveLength(2);
  expect(JSON.stringify(reads[0].Resource)).toContain(":parameter/flowcast/api-data-gov-key");
  const decrypts = statements.filter((s: any) => s.Action === "kms:Decrypt");
  expect(decrypts).toHaveLength(2);
  expect(decrypts[0].Condition.StringEquals["kms:ViaService"]).toBe("ssm.eu-west-1.amazonaws.com");
});

test("no VPC, NAT or public IPs", () => {
  template.resourceCountIs("AWS::EC2::VPC", 0);
  template.resourceCountIs("AWS::EC2::NatGateway", 0);
});
