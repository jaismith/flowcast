import * as cdk from 'aws-cdk-lib';
import { Stack, StackProps } from 'aws-cdk-lib';
import * as ddb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as events from 'aws-cdk-lib/aws-events';
import * as eventTargets from 'aws-cdk-lib/aws-events-targets';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as sfnTasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as certificatemanager from 'aws-cdk-lib/aws-certificatemanager';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as batch from 'aws-cdk-lib/aws-batch';
import  { Nextjs } from 'cdk-nextjs-standalone';
import { Construct } from 'constructs';
import * as path from 'path';
import * as dotenv from 'dotenv';

dotenv.config();

const DOMAIN_NAME = 'flowcast.jaismith.dev';
const WEB_APP_DOMAIN = DOMAIN_NAME;
const DEFAULT_LOG_RETENTION = logs.RetentionDays.ONE_MONTH;

export class FlowcastStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // * infra

    // * db
    const db = new ddb.Table(this, 'flowcast-data', {
      tableName: 'flowcast-data',
      billingMode: ddb.BillingMode.PAY_PER_REQUEST,
      partitionKey: { name: 'usgs_site#type', type: ddb.AttributeType.STRING },
      sortKey: { name: 'origin#timestamp', type: ddb.AttributeType.STRING },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecovery: true
    });
    db.addGlobalSecondaryIndex({
      indexName: 'fcst_horizon_aware_index',
      partitionKey: { name: 'usgs_site#type', type: ddb.AttributeType.STRING },
      sortKey: { name: 'horizon#timestamp', type: ddb.AttributeType.STRING }
    });

    // reports table
    const reportsDb = new ddb.Table(this, 'flowcast-reports', {
      tableName: 'flowcast-reports',
      billingMode: ddb.BillingMode.PAY_PER_REQUEST,
      partitionKey: { name: 'usgs_site', type: ddb.AttributeType.STRING },
      sortKey: { name: 'date', type: ddb.AttributeType.STRING },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecovery: true
    });

    // sites table
    const sitesDb = new ddb.Table(this, 'flowcast-sites', {
      tableName: 'flowcast-sites',
      billingMode: ddb.BillingMode.PAY_PER_REQUEST,
      partitionKey: { name: 'usgs_site', type: ddb.AttributeType.STRING },
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecovery: true,
      stream: ddb.StreamViewType.NEW_IMAGE
    });

    // s3 buckets
    const jumpstartBucket = new s3.Bucket(this, 'jumpstart-bucket');
    const archiveBucket = new s3.Bucket(this, 'archive-bucket');
    const modelBucket = new s3.Bucket(this, 'model-bucket');

    // * lambda

    const env = {
      WEATHER_KEY: process.env.WEATHER_KEY!,
      RDS_PASS: process.env.RDS_PASS!,
      RDS_HOST: process.env.RDS_HOST!,
      NCEI_HOST: process.env.NCEI_HOST!,
      NCEI_EMAIL: process.env.NCEI_EMAIL!,
      VISUAL_CROSSING_API_KEY: process.env.VISUAL_CROSSING_API_KEY!,
      DATA_TABLE_ARN: db.tableArn,
      JUMPSTART_BUCKET_NAME: jumpstartBucket.bucketName,
      ARCHIVE_BUCKET_NAME: archiveBucket.bucketName,
      MODEL_BUCKET_NAME: modelBucket.bucketName
    };

    const sharedLambdaImage = new ecrAssets.DockerImageAsset(this, 'shared_lambda_image', {
      directory: path.join(__dirname, '../../backend'),
      platform: ecrAssets.Platform.LINUX_AMD64
    });

    const update = new lambda.DockerImageFunction(this, 'update_function', {
      code: lambda.DockerImageCode.fromEcr(sharedLambdaImage.repository, {
        tagOrDigest: sharedLambdaImage.imageTag,
        entrypoint: ['python', '-m', 'awslambdaric'],
        cmd: ['index.handle_update'],
      }),
      environment: env,
      architecture: lambda.Architecture.X86_64,
      timeout: cdk.Duration.minutes(5),
      memorySize: 1024,
      logRetention: DEFAULT_LOG_RETENTION
    });
    const forecast = new lambda.DockerImageFunction(this, 'forecast_function', {
      code: lambda.DockerImageCode.fromEcr(sharedLambdaImage.repository, {
        tagOrDigest: sharedLambdaImage.imageTag,
        entrypoint: ['python', '-m', 'awslambdaric'],
        cmd: ['index.handle_forecast'],
      }),
      environment: env,
      architecture: lambda.Architecture.X86_64,
      timeout: cdk.Duration.minutes(5),
      memorySize: 2048,
      logRetention: DEFAULT_LOG_RETENTION
    });
    const access = new lambda.DockerImageFunction(this, 'access_function', {
      code: lambda.DockerImageCode.fromEcr(sharedLambdaImage.repository, {
        tagOrDigest: sharedLambdaImage.imageTag,
        entrypoint: ['python', '-m', 'awslambdaric'],
        cmd: ['index.handle_access'],
      }),
      environment: env,
      architecture: lambda.Architecture.X86_64,
      timeout: cdk.Duration.minutes(1),
      memorySize: 1024,
      logRetention: DEFAULT_LOG_RETENTION
    });
    access.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:InvokeModel'],
      resources: ['*']
    }));
    const exportFunc = new lambda.DockerImageFunction(this, 'export_function', {
      code: lambda.DockerImageCode.fromEcr(sharedLambdaImage.repository, {
        tagOrDigest: sharedLambdaImage.imageTag,
        entrypoint: ['python', '-m', 'awslambdaric'],
        cmd: ['index.handle_export'],
      }),
      environment: env,
      architecture: lambda.Architecture.X86_64,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: DEFAULT_LOG_RETENTION
    });

    const onboardConnect = new lambda.DockerImageFunction(this, 'onboard_connect_function', {
      code: lambda.DockerImageCode.fromEcr(sharedLambdaImage.repository, {
        tagOrDigest: sharedLambdaImage.imageTag,
        entrypoint: ['python', '-m', 'awslambdaric'],
        cmd: ['index.handle_onboard_connect']
      }),
      environment: env,
      architecture: lambda.Architecture.X86_64,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: DEFAULT_LOG_RETENTION
    });
    const onboardDisconnect = new lambda.DockerImageFunction(this, 'onboard_disconnect_function', {
      code: lambda.DockerImageCode.fromEcr(sharedLambdaImage.repository, {
        tagOrDigest: sharedLambdaImage.imageTag,
        entrypoint: ['python', '-m', 'awslambdaric'],
        cmd: ['index.handle_onboard_disconnect']
      }),
      environment: env,
      architecture: lambda.Architecture.X86_64,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: DEFAULT_LOG_RETENTION
    });

    const websocketApi = new apigatewayv2.WebSocketApi(this, 'flowcast-websocket-api', {
      connectRouteOptions: { integration: new integrations.WebSocketLambdaIntegration('connect-integration', onboardConnect) },
      disconnectRouteOptions: { integration: new integrations.WebSocketLambdaIntegration('disconnect-integration', onboardDisconnect) }
    });

    const websocketApiStage = new apigatewayv2.WebSocketStage(this, 'flowcast-websocket-api-stage', {
      webSocketApi: websocketApi,
      stageName: 'prod',
      autoDeploy: true
    });

    const onboardProcessStream = new lambda.DockerImageFunction(this, 'onboard_process_stream', {
      code: lambda.DockerImageCode.fromEcr(sharedLambdaImage.repository, {
        tagOrDigest: sharedLambdaImage.imageTag,
        entrypoint: ['python', '-m', 'awslambdaric'],
        cmd: ['index.handle_onboard_process_stream']
      }),
      environment: {
        ...env,
        WEBSOCKET_API_ENDPOINT: websocketApiStage.url
      },
      architecture: lambda.Architecture.X86_64,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: DEFAULT_LOG_RETENTION
    });
    onboardProcessStream.addEventSource(new lambdaEventSources.DynamoEventSource(sitesDb, {
      startingPosition: lambda.StartingPosition.TRIM_HORIZON,
      retryAttempts: 3
    }));

    // * fargate

    const trainVpc = new ec2.Vpc(this, 'flowcast-vpc', {
      maxAzs: 1,
      natGateways: 0,
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'public-subnet',
          subnetType: ec2.SubnetType.PUBLIC,
        }
      ]
    });

    const trainComputeEnv = new batch.FargateComputeEnvironment(this, 'flowcast-batch-fargate-spot-environment', {
      spot: true,
      maxvCpus: 16,
      vpc: trainVpc,
      vpcSubnets: trainVpc.selectSubnets({
        subnetType: ec2.SubnetType.PUBLIC
      })
    });

    const trainJobQueue = new batch.JobQueue(this, 'flowcast-batch-job-queue', {
      computeEnvironments: [{
        computeEnvironment: trainComputeEnv,
        order: 1
      }]
    });

    const trainLogs = new logs.LogGroup(this, 'flowcast-train-loggroup', {
      logGroupName: 'flowcast-train-fargate-loggroup',
      retention: DEFAULT_LOG_RETENTION,
      removalPolicy: cdk.RemovalPolicy.DESTROY
    });

    const trainRole = new iam.Role(this, 'flowcast-train-role', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com')
    });

    const trainJobDefinition = new batch.EcsJobDefinition(this, 'flowcast-batch-job-def', {
      container: new batch.EcsFargateContainerDefinition(this, 'flowcast-batch-job-container-def', {
        image: ecs.ContainerImage.fromEcrRepository(sharedLambdaImage.repository, sharedLambdaImage.imageTag),
        command: ['python', '-c', 'import sys; from index import handle_train; handle_train(sys.argv[1])', 'Ref::usgs_site'],
        memory: cdk.Size.gibibytes(32),
        cpu: 16,
        environment: env,
        logging: new ecs.AwsLogDriver({
          streamPrefix: 'ecs',
          logGroup: trainLogs
        }),
        jobRole: trainRole,
        assignPublicIp: true
      })
    });
    trainJobDefinition.container.executionRole.addToPrincipalPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ],
      resources: ["*"]
    }));

    // * permissions

    [update, forecast, access, exportFunc, onboardConnect, onboardDisconnect, onboardProcessStream].forEach(func => {
      db.grantFullAccess(func);
      reportsDb.grantFullAccess(func);
      sitesDb.grantFullAccess(func);
    });
    jumpstartBucket.grantReadWrite(update);
    archiveBucket.grantReadWrite(trainRole);
    modelBucket.grantReadWrite(trainRole);
    modelBucket.grantReadWrite(forecast);

    onboardProcessStream.addToRolePolicy(new iam.PolicyStatement({
      actions: ['execute-api:ManageConnections'],
      resources: ['*']
    }));

    exportFunc.addToRolePolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'dynamodb:ExportTableToPointInTime',
        'dynamodb:DescribeExport',
        'dynamodb:ListExports',
        'dynamodb:DeleteExport'
      ],
      resources: ['*'],
    }));
    archiveBucket.grantReadWrite(exportFunc);

    sitesDb.grantFullAccess(trainRole);

    // * sfn

    const updateTask = new sfnTasks.LambdaInvoke(this, 'update_task', {
      lambdaFunction: update,
      resultPath: '$.Result'
    });
    const forecastTask = new sfnTasks.LambdaInvoke(this, 'forecast_task', {
      lambdaFunction: forecast,
      resultPath: '$.Result'
    });
    const exportTask = new sfnTasks.LambdaInvoke(this, 'export_task', {
      lambdaFunction: exportFunc,
      resultPath: '$.Result'
    });
    const trainTask = new sfnTasks.BatchSubmitJob(this, 'train_task', {
      jobQueueArn: trainJobQueue.jobQueueArn,
      jobDefinitionArn: trainJobDefinition.jobDefinitionArn,
      jobName: 'site_onboarding_initial_train',
      payload: sfn.TaskInput.fromObject({
        'usgs_site.$': '$.usgs_site'
      }),
      resultPath: '$.Result'
    });

    const wait = new sfn.Wait(this, 'wait', {
      time: sfn.WaitTime.duration(cdk.Duration.seconds(30))
    });
    const onboardCondition = sfn.Condition.booleanEquals('$.is_onboarding', true);
    const failCondition = sfn.Condition.not(sfn.Condition.numberEquals('$.Result.Payload.statusCode', 200));

    const exportCompleteChoice = new sfn.Choice(this, 'check_export_complete');
    exportCompleteChoice
      .when(sfn.Condition.numberEquals('$.Result.Payload.statusCode', 200), new sfn.Pass(this, 'export_complete'))
      .when(sfn.Condition.numberGreaterThanEquals('$.Result.Payload.statusCode', 400), new sfn.Fail(this, 'export_failed'))
      .otherwise(wait
        .next(new sfnTasks.LambdaInvoke(this, 'poll_export_task', {
          lambdaFunction: exportFunc,
          resultPath: '$.Result'
        }))
        .next(exportCompleteChoice));

    const updateAndForecastSfn = new sfn.StateMachine(this, 'update_and_forecast', {
      definitionBody: sfn.DefinitionBody.fromChainable(sfn.Chain.start(updateTask)
        .next(new sfn.Choice(this, 'verify_update')
          .when(failCondition, new sfn.Fail(this, 'update_failed'))
          .otherwise(new sfn.Pass(this, 'update_successful'))
          .afterwards())
        .next(new sfn.Choice(this, 'check_onboarding')
          .when(onboardCondition, exportTask
            .next(exportCompleteChoice.afterwards())
            .next(trainTask)
            .next(new sfn.Choice(this, 'verify_train')
              .when(failCondition, new sfn.Fail(this, 'train_failed'))
              .otherwise(new sfn.Pass(this, 'train_successful'))
              .afterwards()))
          .otherwise(new sfn.Pass(this, 'not_onboarding'))
          .afterwards())
        .next(forecastTask)
        .next(new sfn.Choice(this, 'verify_forecast')
          .when(failCondition, new sfn.Fail(this, 'forecast_failed'))
          .otherwise(new sfn.Pass(this, 'forecast_successful'))
          .afterwards())
        .next(new sfn.Succeed(this, 'update_and_forecast_successful'))),
      timeout: cdk.Duration.minutes(25)
    });

    // * public access url

    new cdk.CfnResource(this, 'public_access_url', {
      type: 'AWS::Lambda::Url',
      properties: {
        TargetFunctionArn: access.functionArn,
        AuthType: 'NONE',
        Cors: {
          AllowOrigins: ['*'],
          AllowMethods: ['GET'],
          MaxAge: 3600
        },
      }
    });
    new cdk.CfnResource(this, 'public_access_url_permission', {
      type: 'AWS::Lambda::Permission',
      properties: {
        FunctionName: access.functionName,
        Principal: '*',
        Action: 'lambda:InvokeFunctionUrl',
        FunctionUrlAuthType: 'NONE'
      }
    });

    const hostedZone = route53.HostedZone.fromLookup(this, 'hosted-zone', {
      domainName: DOMAIN_NAME
    });
    const certificate = new certificatemanager.Certificate(this, 'api-certificate', {
      domainName: `api.${DOMAIN_NAME}`,
      validation: certificatemanager.CertificateValidation.fromDns(hostedZone)
    });
    const publicApi = new apigateway.LambdaRestApi(this, 'public-api', {
      handler: access,
      domainName: {
        domainName: `api.${DOMAIN_NAME}`,
        certificate,
        endpointType: apigateway.EndpointType.REGIONAL
      }
    });
    new route53.ARecord(this, 'api-dns-record', {
      zone: hostedZone,
      recordName: 'api',
      target: route53.RecordTarget.fromAlias(new route53Targets.ApiGateway(publicApi))
    });

    // * cron

    const hourly = new events.Rule(this, 'hourly', {
      schedule: events.Schedule.expression('cron(5 * * * ? *)')
    });
    hourly.addTarget(new eventTargets.SfnStateMachine(updateAndForecastSfn, {
      input: events.RuleTargetInput.fromObject({ 'usgs_site': '01427510', 'is_onboarding': false })
    }));
    const weekly = new events.Rule(this, 'weekly', {
      schedule: events.Schedule.expression('cron(0 0 ? * SUN *)')
    });
    weekly.addTarget(new eventTargets.LambdaFunction(exportFunc));

    // * client

    const client = new Nextjs(this, 'nextjs-client', {
      nextjsPath: path.join(__dirname, '../../client'),
      buildCommand: 'yarn workspace @flowcast/client run build',
      buildPath: path.join(__dirname, '../..'),
      domainProps: {
        domainName: WEB_APP_DOMAIN,
        hostedZone: hostedZone
      }
    });
    new cdk.CfnOutput(this, "nextjs-client-distribution-domain", {
      value: client.distribution.distributionDomain,
    });
  }
}
