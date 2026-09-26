import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import * as path from "node:path";

import * as cdk from "aws-cdk-lib";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
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

const REPO = path.resolve(__dirname, "..", "..");
const PIPELINE = path.join(REPO, "pipeline");
const EVALUATION = path.join(REPO, "evaluation");

// Supplied by the Lambda runtime (boto3) or the AWS SDK for pandas layer (numpy, pandas, pyarrow, and the
// HTTP stack below at the same versions as uv.lock). Function plus layers must stay under 250 MB unzipped:
// the layer is ~195 MB, so scipy (115 MB) is left out and the skill page loads a saved air2stream fit.
const NOT_BUNDLED = new Set([
  "boto3", "botocore", "s3transfer", "jmespath", "numpy", "pandas", "pyarrow", "scipy",
  "aiohttp", "aiohappyeyeballs", "aiosignal", "attrs", "frozenlist", "multidict", "propcache", "yarl",
  "requests", "certifi", "charset-normalizer", "idna", "python-dateutil", "six", "typing-extensions",
]);

// Published per region by AWS; see https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html
const PANDAS_LAYER_ACCOUNT = "336392948345";
const DEFAULT_PANDAS_LAYER_VERSION = 31;
const SITES_IN_BUNDLE = "/var/task/sites.yaml";

export interface FlowcastV2StackProps extends cdk.StackProps {
  /** Email address subscribed to the alarms; omit for a topic with no subscribers. */
  readonly alertEmail?: string;
  readonly pandasLayerVersion?: number;
}

/**
 * Installs pinned (uv.lock) dependencies of `project` for Lambda arm64 without Docker, then copies the
 * flowcast packages and the site registry into the bundle.
 */
class UvLocalBundling implements cdk.ILocalBundling {
  constructor(
    private readonly project: string,
    private readonly packages: string[],
  ) {}

  tryBundle(outputDir: string): boolean {
    const work = mkdtempSync(path.join(tmpdir(), "flowcast-v2-"));
    try {
      const exported = execFileSync(
        "uv",
        ["export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project", "--no-header", "--no-annotate"],
        { cwd: this.project, encoding: "utf8" },
      );
      const requirements = exported
        .split("\n")
        .map((line) => line.trim())
        // Local path dependencies (`-e ../pipeline`) are copied from source below.
        .filter((line) => line && !line.startsWith("-e") && !line.startsWith("."))
        .filter((line) => !NOT_BUNDLED.has(line.split(/[=<>~; ]/)[0].trim().toLowerCase()));
      const reqFile = path.join(work, "requirements.txt");
      writeFileSync(reqFile, requirements.join("\n") + "\n");
      execFileSync(
        "uv",
        [
          "pip", "install", "--quiet", "--no-deps", "--only-binary", ":all:",
          // The python3.12 runtime is Amazon Linux 2023 (glibc 2.34); h5py only ships manylinux_2_28 arm64 wheels.
          "--python-platform", "aarch64-manylinux_2_28", "--python-version", "3.12",
          "--target", outputDir, "-r", reqFile,
        ],
        { cwd: this.project, stdio: "inherit" },
      );
      for (const pkg of this.packages) {
        cpSync(path.join(REPO, pkg), path.join(outputDir, path.basename(pkg)), {
          recursive: true,
          filter: (src) => !src.includes("__pycache__"),
        });
      }
      cpSync(path.join(PIPELINE, "sites.yaml"), path.join(outputDir, "sites.yaml"));
      return true;
    } finally {
      rmSync(work, { recursive: true, force: true });
    }
  }
}

function hashTree(paths: string[]): string {
  const hash = createHash("sha256");
  const visit = (p: string) => {
    if (p.includes("__pycache__")) return;
    if (statSync(p).isDirectory()) {
      for (const entry of readdirSync(p).sort()) visit(path.join(p, entry));
    } else {
      hash.update(path.relative(REPO, p));
      hash.update(readFileSync(p));
    }
  };
  paths.forEach(visit);
  return hash.digest("hex");
}

function pythonCode(project: string, packages: string[]): lambda.Code {
  const inputs = [
    ...packages.map((pkg) => path.join(REPO, pkg)),
    path.join(PIPELINE, "sites.yaml"),
    path.join(project, "uv.lock"),
    path.join(project, "pyproject.toml"),
  ];
  return lambda.Code.fromAsset(project, {
    assetHashType: cdk.AssetHashType.CUSTOM,
    assetHash: hashTree(inputs),
    bundling: {
      image: lambda.Runtime.PYTHON_3_12.bundlingImage,
      local: new UvLocalBundling(project, packages),
    },
  });
}

export class FlowcastV2Stack extends cdk.Stack {
  readonly lake: s3.Bucket;
  readonly web: s3.Bucket;
  readonly distribution: cloudfront.Distribution;
  readonly ingest: lambda.Function;
  readonly skillPage: lambda.Function;

  constructor(scope: Construct, id: string, props: FlowcastV2StackProps = {}) {
    super(scope, id, props);

    this.lake = new s3.Bucket(this, "Lake", {
      bucketName: `flowcast-v2-lake-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        { id: "expire-run-markers", prefix: "_runs/", expiration: cdk.Duration.days(90) },
        { id: "abort-multipart", abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
      ],
    });

    this.web = new s3.Bucket(this, "Web", {
      bucketName: `flowcast-v2-web-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    this.distribution = new cloudfront.Distribution(this, "SkillSite", {
      comment: "flowcast v2 skill page",
      defaultRootObject: "index.html",
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.web),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        // Honors the objects' Cache-Control (15 minutes), so nightly updates show up without invalidations.
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
        compress: true,
      },
    });

    const pandasLayer = lambda.LayerVersion.fromLayerVersionArn(
      this,
      "AwsSdkPandas",
      `arn:${this.partition}:lambda:${this.region}:${PANDAS_LAYER_ACCOUNT}:layer:AWSSDKPandas-Python312-Arm64:` +
        `${props.pandasLayerVersion ?? DEFAULT_PANDAS_LAYER_VERSION}`,
    );
    const common = {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      layers: [pandasLayer],
      // One run at a time: runs merge into shared monthly files and a shared cache snapshot.
      reservedConcurrentExecutions: 1,
      // The next scheduled run catches up (the ingest re-pulls a 12 h window).
      retryAttempts: 0,
    };

    this.ingest = new lambda.Function(this, "ObsIngest", {
      ...common,
      functionName: "flowcast-obs-ingest",
      description: "Hourly USGS discharge, water temperature and stage for the Upper Delaware gauges -> S3 Parquet",
      handler: "flowcast_pipeline.lambda_handler.handler",
      code: pythonCode(PIPELINE, ["pipeline/src/flowcast_pipeline"]),
      memorySize: 512,
      timeout: cdk.Duration.minutes(5),
      environment: {
        LAKE_URI: `s3://${this.lake.bucketName}`,
        FLOWCAST_SITES: SITES_IN_BUNDLE,
        FLOWCAST_CACHE_DIR: "/tmp/flowcast-cache",
      },
      logGroup: new logs.LogGroup(this, "ObsIngestLogs", {
        logGroupName: "/aws/lambda/flowcast-obs-ingest",
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    this.lake.grantReadWrite(this.ingest);
    cdk.Tags.of(this.ingest).add("component", "obs-ingest");

    const archiveBucketName = `flowcast-archiver-${this.account}-${this.region}`;
    this.skillPage = new lambda.Function(this, "SkillPage", {
      ...common,
      functionName: "flowcast-skill-page",
      description: "Nightly: scores baselines, NWM and the forecast archive with the evaluation harness; publishes the skill page",
      handler: "flowcast_eval.lambda_handler.handler",
      code: pythonCode(EVALUATION, ["pipeline/src/flowcast_pipeline", "evaluation/src/flowcast_eval"]),
      // A full run peaks under 1 GB.
      memorySize: 2048,
      timeout: cdk.Duration.minutes(15),
      ephemeralStorageSize: cdk.Size.gibibytes(2),
      environment: {
        SITE_ID: "USGS-01427510",
        LAKE_URI: `s3://${this.lake.bucketName}`,
        ARCHIVE_URI: `s3://${archiveBucketName}/baselines`,
        WEB_URI: `s3://${this.web.bucketName}`,
        FLOWCAST_SITES: SITES_IN_BUNDLE,
        FLOWCAST_CACHE_DIR: "/tmp/flowcast-cache",
      },
      logGroup: new logs.LogGroup(this, "SkillPageLogs", {
        logGroupName: "/aws/lambda/flowcast-skill-page",
        retention: logs.RetentionDays.TWO_WEEKS,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    this.lake.grantReadWrite(this.skillPage);
    this.web.grantReadWrite(this.skillPage);
    // Read-only access to the archiver's bucket; the archiver stack itself is not modified.
    s3.Bucket.fromBucketName(this, "Archive", archiveBucketName).grantRead(this.skillPage, "baselines/*");
    cdk.Tags.of(this.skillPage).add("component", "skill-page");

    new scheduler.Schedule(this, "IngestHourly", {
      scheduleName: "flowcast-obs-ingest-hourly",
      description: "Hourly observation ingest (12 h trailing window)",
      schedule: scheduler.ScheduleExpression.cron({ minute: "10", hour: "*" }),
      target: new targets.LambdaInvoke(this.ingest, { retryAttempts: 0 }),
    });
    new scheduler.Schedule(this, "IngestRevisions", {
      scheduleName: "flowcast-obs-ingest-revisions-weekly",
      description: "Weekly 30-day re-pull to pick up USGS revisions to provisional data",
      schedule: scheduler.ScheduleExpression.cron({ minute: "40", hour: "4", weekDay: "MON" }),
      target: new targets.LambdaInvoke(this.ingest, {
        retryAttempts: 0,
        input: scheduler.ScheduleTargetInput.fromObject({ window_hours: 720 }),
      }),
    });
    new scheduler.Schedule(this, "SkillPageDaily", {
      scheduleName: "flowcast-skill-page-daily",
      description: "Nightly scoring run and skill page publish",
      // After the 00Z NWM cycle is complete on noaa-nwm-pds and the morning MARFC bulletin is archived.
      schedule: scheduler.ScheduleExpression.cron({ minute: "30", hour: "15" }),
      target: new targets.LambdaInvoke(this.skillPage, { retryAttempts: 0 }),
    });

    const alerts = new sns.Topic(this, "Alerts", { topicName: "flowcast-v2-alerts" });
    if (props.alertEmail) {
      alerts.addSubscription(new subscriptions.EmailSubscription(props.alertEmail));
    }
    const alarm = (id: string, name: string, description: string, metric: cloudwatch.Metric, overrides: Partial<cloudwatch.CreateAlarmOptions> = {}) =>
      metric
        .createAlarm(this, id, {
          alarmName: name,
          alarmDescription: description,
          threshold: 1,
          evaluationPeriods: 1,
          comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
          treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
          ...overrides,
        })
        .addAlarmAction(new cwActions.SnsAction(alerts));
    const hourlySum = { period: cdk.Duration.hours(1), statistic: cloudwatch.Stats.SUM };
    alarm("IngestErrors", "flowcast-obs-ingest-errors", "An obs ingest run failed; check /aws/lambda/flowcast-obs-ingest",
      this.ingest.metricErrors(hourlySum));
    alarm("IngestMissing", "flowcast-obs-ingest-missing", "No obs ingest run for 2 hours (Phase 0 allows <= 1% missed cycles)",
      this.ingest.metricInvocations(hourlySum), {
        evaluationPeriods: 2,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
      });
    alarm("SkillPageErrors", "flowcast-skill-page-errors", "The nightly skill page run failed; check /aws/lambda/flowcast-skill-page",
      this.skillPage.metricErrors(hourlySum));

    new cdk.CfnOutput(this, "SkillPageUrl", { value: `https://${this.distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, "LakeUri", { value: `s3://${this.lake.bucketName}` });
    new cdk.CfnOutput(this, "WebBucket", { value: this.web.bucketName });
    new cdk.CfnOutput(this, "AlertTopic", { value: alerts.topicName });
  }
}
