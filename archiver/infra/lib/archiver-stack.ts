import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import * as path from "node:path";

import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cwActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import * as targets from "aws-cdk-lib/aws-scheduler-targets";
import * as sns from "aws-cdk-lib/aws-sns";
import * as subscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import { Construct } from "constructs";

const PROJECT_DIR = path.resolve(__dirname, "..", "..");

// Supplied by the Lambda runtime (boto3) or the AWS SDK for pandas layer (the rest).
const NOT_BUNDLED = new Set(["boto3", "botocore", "s3transfer", "numpy", "pandas", "pyarrow"]);

// Published per region by AWS; see https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html
const PANDAS_LAYER_ACCOUNT = "336392948345";
const DEFAULT_PANDAS_LAYER_VERSION = 31;

export interface ArchiverStackProps extends cdk.StackProps {
  /** Email address subscribed to the error alarm; omit for a topic with no subscribers. */
  readonly alertEmail?: string;
  readonly pandasLayerVersion?: number;
}

/** Installs the archiver and its pinned (uv.lock) dependencies for Lambda arm64 without Docker. */
class UvLocalBundling implements cdk.ILocalBundling {
  tryBundle(outputDir: string): boolean {
    const work = mkdtempSync(path.join(tmpdir(), "flowcast-archiver-"));
    try {
      const exported = execFileSync(
        "uv",
        ["export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project", "--no-header", "--no-annotate"],
        { cwd: PROJECT_DIR, encoding: "utf8" },
      );
      const requirements = exported
        .split("\n")
        .filter((line) => line.trim() && !NOT_BUNDLED.has(line.split(/[=<>~; ]/)[0].trim().toLowerCase()));
      const reqFile = path.join(work, "requirements.txt");
      writeFileSync(reqFile, requirements.join("\n") + "\n");
      execFileSync(
        "uv",
        [
          "pip", "install", "--quiet", "--no-deps", "--only-binary", ":all:",
          "--python-platform", "aarch64-manylinux2014", "--python-version", "3.12",
          "--target", outputDir, "-r", reqFile,
        ],
        { cwd: PROJECT_DIR, stdio: "inherit" },
      );
      cpSync(path.join(PROJECT_DIR, "src", "flowcast_archiver"), path.join(outputDir, "flowcast_archiver"), {
        recursive: true,
        filter: (src) => !src.includes("__pycache__"),
      });
      return true;
    } finally {
      rmSync(work, { recursive: true, force: true });
    }
  }
}

export class ArchiverStack extends cdk.Stack {
  readonly bucket: s3.Bucket;
  readonly fn: lambda.Function;

  constructor(scope: Construct, id: string, props: ArchiverStackProps = {}) {
    super(scope, id, props);

    this.bucket = new s3.Bucket(this, "Archive", {
      bucketName: `flowcast-archiver-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // Issued forecasts can't be re-fetched once NWS drops them; versioning guards against overwrites.
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          id: "raw-to-glacier-ir",
          prefix: "baselines/raw/",
          transitions: [{ storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL, transitionAfter: cdk.Duration.days(90) }],
        },
        { id: "expire-noncurrent", noncurrentVersionExpiration: cdk.Duration.days(30) },
        { id: "abort-multipart", abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
      ],
    });

    const logGroup = new logs.LogGroup(this, "Logs", {
      logGroupName: "/aws/lambda/flowcast-archiver",
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const pandasLayer = lambda.LayerVersion.fromLayerVersionArn(
      this,
      "AwsSdkPandas",
      `arn:${this.partition}:lambda:${this.region}:${PANDAS_LAYER_ACCOUNT}:layer:AWSSDKPandas-Python312-Arm64:` +
        `${props.pandasLayerVersion ?? DEFAULT_PANDAS_LAYER_VERSION}`,
    );

    this.fn = new lambda.Function(this, "Archiver", {
      functionName: "flowcast-archiver",
      description: "Archives NWS/NOAA benchmark forecasts (MARFC, HEFS, NWM, USGS temperature) to S3",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "flowcast_archiver.lambda_handler.handler",
      code: lambda.Code.fromAsset(PROJECT_DIR, {
        exclude: [".venv", "lake", "infra", "tests", "**/__pycache__", ".pytest_cache", "*.tar"],
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          local: new UvLocalBundling(),
        },
      }),
      layers: [pandasLayer],
      memorySize: 1024,
      timeout: cdk.Duration.minutes(10),
      environment: { ARCHIVE_URI: `s3://${this.bucket.bucketName}/baselines` },
      logGroup,
      // One run at a time: each run rewrites the shared state file.
      reservedConcurrentExecutions: 1,
      // A failed run has already saved what it collected; the next hourly run picks up the rest.
      retryAttempts: 0,
    });
    this.bucket.grantReadWrite(this.fn);

    new scheduler.Schedule(this, "Hourly", {
      scheduleName: "flowcast-archiver-hourly",
      description: "Hourly flowcast baseline forecast archive run",
      schedule: scheduler.ScheduleExpression.cron({ minute: "20", hour: "*" }),
      target: new targets.LambdaInvoke(this.fn, { retryAttempts: 0 }),
    });

    const alerts = new sns.Topic(this, "Alerts", { topicName: "flowcast-archiver-alerts" });
    if (props.alertEmail) {
      alerts.addSubscription(new subscriptions.EmailSubscription(props.alertEmail));
    }
    const errors = this.fn
      .metricErrors({ period: cdk.Duration.hours(1), statistic: cloudwatch.Stats.SUM })
      .createAlarm(this, "ErrorAlarm", {
        alarmName: "flowcast-archiver-errors",
        alarmDescription: "A flowcast-archiver run failed or timed out; check /aws/lambda/flowcast-archiver",
        threshold: 1,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    errors.addAlarmAction(new cwActions.SnsAction(alerts));

    new cdk.CfnOutput(this, "BucketName", { value: this.bucket.bucketName });
    new cdk.CfnOutput(this, "ArchiveUri", { value: `s3://${this.bucket.bucketName}/baselines` });
    new cdk.CfnOutput(this, "FunctionName", { value: this.fn.functionName });
  }
}
