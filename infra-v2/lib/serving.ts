import * as path from "node:path";
import { readFileSync } from "node:fs";

import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cwActions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import * as targets from "aws-cdk-lib/aws-scheduler-targets";
import * as sns from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";

import { pythonCode } from "./flowcast-v2-stack";

const REPO = path.resolve(__dirname, "..", "..");
const SERVING = path.join(REPO, "serving");
const PANDAS_LAYER_ACCOUNT = "336392948345";
const PANDAS_LAYER_VERSION = 31;
const API_KEY_PARAMETER = "/flowcast/api-data-gov-key";
const SITES_IN_BUNDLE = "/var/task/sites.yaml";
const DOMAIN = "flowcast.jaismith.dev";
// The page's single CloudFront rewrite (from the web app, PR #65): extensionless routes -> index.html.
const SPA_REWRITE = path.join(REPO, "web", "deploy", "spa-rewrite.js");

export interface FlowcastServeStackProps extends cdk.StackProps {
  /** Route 53 zone for flowcast.jaismith.dev (certificate DNS validation only; no records point here yet). */
  readonly hostedZoneId: string;
  /** Existing SNS topic for alarms (flowcast-v2-alerts). */
  readonly alertTopicName: string;
  /**
   * Attach flowcast.jaismith.dev to the distribution and point the zone's apex A/AAAA aliases at it. Only after the
   * legacy flowcast-stack (which owns those records and the domain on its distribution) is deleted: `-c cutover=true`.
   */
  readonly cutover?: boolean;
}

/**
 * Production serving (docs: serving/README.md; design: production-architecture.md): the site and data buckets
 * behind one CloudFront distribution, the visit/status API, the control table, the forecast container function,
 * the ops function (cycles and the hourly light build), schedules and alarms.
 */
export class FlowcastServeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FlowcastServeStackProps) {
    super(scope, id, props);
    const lake = s3.Bucket.fromBucketName(this, "Lake", `flowcast-v2-lake-${this.account}-${this.region}`);
    const lakeUri = `s3://${lake.bucketName}`;

    const site = new s3.Bucket(this, "Site", {
      bucketName: `flowcast-site-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    const data = new s3.Bucket(this, "Data", {
      bucketName: `flowcast-data-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        // Forecast JSON is tagged at write; the lake keeps the record (forecasts/live/).
        { id: "expire-forecast-json", tagFilters: { retention: "30d" }, expiration: cdk.Duration.days(30) },
        { id: "abort-multipart", abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
      ],
    });

    const table = new dynamodb.TableV2(this, "Control", {
      tableName: "flowcast-control",
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const logGroup = (id: string, name: string) =>
      new logs.LogGroup(this, id, { logGroupName: `/aws/lambda/${name}`, retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: cdk.RemovalPolicy.DESTROY });
    const env = {
      LAKE_URI: lakeUri,
      DATA_BUCKET: data.bucketName,
      CONTROL_TABLE: table.tableName,
      FORECAST_FUNCTION: "flowcast-forecast",
      API_DATA_GOV_KEY_PARAMETER: API_KEY_PARAMETER,
    };

    // ------------------------------------------------------------------ forecast (container)
    const image = new ecrAssets.DockerImageAsset(this, "ForecastImage", {
      directory: REPO,
      file: "serving/Dockerfile",
      platform: ecrAssets.Platform.LINUX_ARM64,
    });
    const forecast = new lambda.DockerImageFunction(this, "Forecast", {
      functionName: "flowcast-forecast",
      description: "Forecast runs (cycles and wakes): extraction, flow + temperature models, calibration, SNOW-17, page JSON; daily SNODAS",
      code: lambda.DockerImageCode.fromEcr(image.repository, { tagOrDigest: image.imageTag }),
      architecture: lambda.Architecture.ARM_64,
      memorySize: 3008,
      timeout: cdk.Duration.minutes(15),
      ephemeralStorageSize: cdk.Size.gibibytes(2),
      environment: { ...env },
      retryAttempts: 0,
      logGroup: logGroup("ForecastLogs", "flowcast-forecast"),
    });

    // ------------------------------------------------------------------ ops (zip): cycles + hourly light build
    const pandasLayer = lambda.LayerVersion.fromLayerVersionArn(
      this, "AwsSdkPandas", `arn:${this.partition}:lambda:${this.region}:${PANDAS_LAYER_ACCOUNT}:layer:AWSSDKPandas-Python312-Arm64:${PANDAS_LAYER_VERSION}`,
    );
    const serveCode = pythonCode(SERVING, ["serving/src/flowcast_serve", "pipeline/src/flowcast_pipeline"]);
    const ops = new lambda.Function(this, "Ops", {
      functionName: "flowcast-serve-ops",
      description: "Scheduled forecast cycles (00/06/12/18Z issues) and the hourly light build (live.json, sites.json)",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      layers: [pandasLayer],
      handler: "flowcast_serve.ops_handler.handler",
      code: serveCode,
      memorySize: 1024,
      timeout: cdk.Duration.minutes(5),
      environment: { ...env, FLOWCAST_SITES: SITES_IN_BUNDLE },
      retryAttempts: 0,
      logGroup: logGroup("OpsLogs", "flowcast-serve-ops"),
    });

    // ------------------------------------------------------------------ API (zip): /api/visit, /api/status
    const api = new lambda.Function(this, "Api", {
      functionName: "flowcast-serve-api",
      description: "POST /api/visit and GET /api/status (behind CloudFront)",
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: "flowcast_serve.api.handler",
      code: serveCode,
      memorySize: 512,
      timeout: cdk.Duration.seconds(15),
      reservedConcurrentExecutions: 5,
      environment: { ...env, FLOWCAST_SITES: SITES_IN_BUNDLE },
      logGroup: logGroup("ApiLogs", "flowcast-serve-api"),
    });
    const apiUrl = api.addFunctionUrl({ authType: lambda.FunctionUrlAuthType.AWS_IAM });

    // ------------------------------------------------------------------ permissions
    const apiKeyArn = this.formatArn({ service: "ssm", resource: "parameter", resourceName: API_KEY_PARAMETER.slice(1) });
    for (const fn of [forecast, ops, api]) {
      table.grantReadWriteData(fn);
      lake.grantRead(fn, "models/*");
      lake.grantRead(fn, "sites/*");
    }
    for (const fn of [forecast, ops]) {
      data.grantReadWrite(fn);
      fn.addToRolePolicy(new iam.PolicyStatement({ actions: ["s3:PutObjectTagging"], resources: [data.arnForObjects("*")] }));
      lake.grantReadWrite(fn, "serving/*");
      lake.grantRead(fn);
      fn.addToRolePolicy(new iam.PolicyStatement({ actions: ["ssm:GetParameter"], resources: [apiKeyArn] }));
      fn.addToRolePolicy(new iam.PolicyStatement({
        actions: ["kms:Decrypt"],
        resources: [this.formatArn({ service: "kms", resource: "key", resourceName: "*" })],
        conditions: { StringEquals: { "kms:ViaService": `ssm.${this.region}.amazonaws.com`, "kms:EncryptionContext:PARAMETER_ARN": apiKeyArn } },
      }));
    }
    for (const prefix of ["forcing/*", "forecasts/*", "events/*"]) lake.grantReadWrite(forecast, prefix);
    lake.grantWrite(forecast, "sites/*");
    // onboarding (static.json): the flow model's training cube (statics, training-year climatology) and the batch's
    // own re-invocation with the basins left
    forecast.addToRolePolicy(new iam.PolicyStatement({
      actions: ["s3:GetObject", "s3:ListBucket"],
      resources: [
        `arn:${this.partition}:s3:::flowcast-training-${this.account}-us-east-2`,
        `arn:${this.partition}:s3:::flowcast-training-${this.account}-us-east-2/v1.3/full/trainval.zarr/*`,
      ],
    }));
    forecast.addToRolePolicy(new iam.PolicyStatement({
      actions: ["lambda:InvokeFunction"],
      resources: [this.formatArn({ service: "lambda", resource: "function", resourceName: "flowcast-forecast", arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME })],
    }));
    // the daily gauge job writes the rule's verdict into the site index
    lake.grantWrite(ops, "sites/index.json");
    forecast.addToRolePolicy(new iam.PolicyStatement({
      actions: ["events:PutEvents"], resources: [this.formatArn({ service: "events", resource: "event-bus", resourceName: "default" })],
    }));
    forecast.grantInvoke(api);
    data.grantRead(api, "data/v1/gauges/ids.json");
    forecast.grantInvoke(ops);

    // ------------------------------------------------------------------ CloudFront
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, "Zone", { hostedZoneId: props.hostedZoneId, zoneName: DOMAIN });
    const certificate = new acm.Certificate(this, "Certificate", { domainName: DOMAIN, validation: acm.CertificateValidation.fromDns(zone) });
    const rewrite = new cloudfront.Function(this, "SpaRewrite", {
      functionName: "flowcast-spa-rewrite",
      comment: "Page routes (no file extension) -> index.html; /preview/<name>/... -> that preview's index.html",
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(readFileSync(SPA_REWRITE, "utf8")),
    });
    const dataCache = new cloudfront.CachePolicy(this, "DataCache", {
      cachePolicyName: "flowcast-data",
      comment: "Honors each object's Cache-Control (live files 60 s, issued forecasts immutable); 60 s when absent",
      defaultTtl: cdk.Duration.seconds(60),
      minTtl: cdk.Duration.seconds(0),
      maxTtl: cdk.Duration.days(365),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
    });
    const headers = new cloudfront.ResponseHeadersPolicy(this, "Headers", {
      responseHeadersPolicyName: "flowcast-security-headers",
      securityHeadersBehavior: {
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: true, override: true },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
        xssProtection: { protection: true, modeBlock: true, override: true },
      },
    });
    const dist = new cloudfront.Distribution(this, "Distribution", {
      comment: "flowcast site, data and API",
      ...(props.cutover ? { domainNames: [DOMAIN], certificate } : {}),
      defaultRootObject: "index.html",
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(site),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: headers,
        compress: true,
        functionAssociations: [{ function: rewrite, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST }],
      },
      additionalBehaviors: {
        "/data/*": {
          // LIST lets S3 answer 404 (not 403) for a missing key; listings are unreachable (query strings aren't forwarded)
          origin: origins.S3BucketOrigin.withOriginAccessControl(data, { originAccessLevels: [cloudfront.AccessLevel.READ, cloudfront.AccessLevel.LIST] }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: dataCache,
          responseHeadersPolicy: headers,
          compress: true,
        },
        "/api/*": {
          origin: origins.FunctionUrlOrigin.withOriginAccessControl(apiUrl),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          responseHeadersPolicy: headers,
        },
      },
    });

    if (props.cutover) {
      const target = route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(dist));
      new route53.ARecord(this, "ApexA", { zone, target });
      new route53.AaaaRecord(this, "ApexAaaa", { zone, target });
    }

    // ------------------------------------------------------------------ schedules
    const invoke = (fn: lambda.IFunction, input: object) =>
      new targets.LambdaInvoke(fn, { retryAttempts: 0, input: scheduler.ScheduleTargetInput.fromObject(input) });
    new scheduler.Schedule(this, "Cycle", {
      scheduleName: "flowcast-forecast-cycle",
      description: "Forecast cycle for the latest synoptic issue (00/06/12/18Z + 1.5 h) for every active site",
      schedule: scheduler.ScheduleExpression.cron({ minute: "30", hour: "1,7,13,19" }),
      target: invoke(ops, { action: "cycle" }),
    });
    new scheduler.Schedule(this, "Light", {
      scheduleName: "flowcast-light-hourly",
      description: "Hourly observations into live.json for active sites, and sites.json",
      schedule: scheduler.ScheduleExpression.cron({ minute: "25", hour: "*" }),
      target: invoke(ops, { action: "light" }),
    });
    new scheduler.Schedule(this, "Gauges", {
      scheduleName: "flowcast-gauges-daily",
      description: "National gauge catalog: every lower-48 discharge gauge with its eligibility and 00060/00010 flags",
      schedule: scheduler.ScheduleExpression.cron({ minute: "15", hour: "6" }),
      target: invoke(ops, { action: "gauges" }),
    });
    for (const [id, hour] of [["SnodasDaily", "13"], ["SnodasRetry", "16"]]) {
      new scheduler.Schedule(this, id, {
        scheduleName: `flowcast-snodas-${hour}30`,
        description: "Daily SNODAS (NSIDC G02158) basin and band SWE for every servable basin",
        schedule: scheduler.ScheduleExpression.cron({ minute: "30", hour }),
        target: invoke(forecast, { action: "snodas" }),
      });
    }

    // ------------------------------------------------------------------ alarms (4 here + 4 elsewhere: within the free 10)
    const topic = sns.Topic.fromTopicArn(this, "Alerts", this.formatArn({ service: "sns", resource: props.alertTopicName }));
    const ns = "flowcast/serving";
    const alarm = (id: string, name: string, description: string, metric: cloudwatch.IMetric, opts: Partial<cloudwatch.CreateAlarmOptions>) =>
      new cloudwatch.Alarm(this, id, {
        alarmName: name, alarmDescription: description, metric, evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        threshold: 1, ...opts,
      }).addAlarmAction(new cwActions.SnsAction(topic));
    alarm("ForecastErrors", "flowcast-forecast-errors", "A forecast run failed or a site failed in it; see /aws/lambda/flowcast-forecast",
      new cloudwatch.MathExpression({
        expression: "errors + failed",
        usingMetrics: {
          errors: forecast.metricErrors({ period: cdk.Duration.hours(1), statistic: "Sum" }),
          failed: new cloudwatch.Metric({ namespace: ns, metricName: "SitesFailed", period: cdk.Duration.hours(1), statistic: "Sum" }),
        },
        period: cdk.Duration.hours(1),
      }), {});
    alarm("ForecastAge", "flowcast-forecast-age", "An always-on site's newest forecast is more than 9 h old",
      new cloudwatch.Metric({ namespace: ns, metricName: "MaxForecastAgeHours", period: cdk.Duration.hours(1), statistic: "Maximum" }),
      { threshold: 9, evaluationPeriods: 2 });
    alarm("LightMissing", "flowcast-light-missing", "No hourly light build for 2 hours",
      new cloudwatch.Metric({ namespace: ns, metricName: "LightBuilds", period: cdk.Duration.hours(1), statistic: "Sum" }),
      { comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD, evaluationPeriods: 2, treatMissingData: cloudwatch.TreatMissingData.BREACHING });
    alarm("WakeLatency", "flowcast-wake-latency", "Wake p95 above 180 s (visit to published forecast)",
      new cloudwatch.Metric({ namespace: ns, metricName: "WakeLatency", period: cdk.Duration.hours(6), statistic: "p95" }),
      { threshold: 180 });

    new cdk.CfnOutput(this, "SiteBucket", { value: site.bucketName });
    new cdk.CfnOutput(this, "DataBucket", { value: data.bucketName });
    new cdk.CfnOutput(this, "DistributionId", { value: dist.distributionId });
    new cdk.CfnOutput(this, "DistributionDomain", { value: dist.distributionDomainName });
    new cdk.CfnOutput(this, "CertificateArn", { value: certificate.certificateArn });
    new cdk.CfnOutput(this, "ControlTable", { value: table.tableName });
    new cdk.CfnOutput(this, "ApiFunctionUrl", { value: apiUrl.url });
  }
}
