import aws_cdk as core
from aws_cdk import (
  aws_dynamodb as ddb,
  aws_lambda,
  aws_events as events,
  aws_events_targets as event_targets,
  aws_ecr_assets as ecr_assets,
  aws_s3 as s3,
  aws_logs,
  aws_stepfunctions as sfn,
  aws_stepfunctions_tasks as sfn_tasks,
  aws_amplify_alpha as aws_amplify,
  aws_iam,
  aws_codebuild,
  aws_apigateway,
  aws_route53,
  aws_route53_targets,
  aws_certificatemanager,
  aws_ec2,
  aws_ecs,
  aws_batch
)
from constructs import Construct
from os import path, environ
from dotenv import load_dotenv
load_dotenv()

DOMAIN_NAME = 'flowcast.jaismith.dev'
WEB_APP_DOMAIN = DOMAIN_NAME
DEFAULT_LOG_RETENTION = aws_logs.RetentionDays.ONE_MONTH

class FlowcastStack(core.Stack):
  def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
    super().__init__(scope, construct_id, **kwargs)

    # * infra

    # * db
    db = ddb.Table(
      self, 'flowcast-data',
      table_name='flowcast-data',
      billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
      partition_key=ddb.Attribute(name='usgs_site#type', type=ddb.AttributeType.STRING),
      sort_key=ddb.Attribute(name='origin#timestamp', type=ddb.AttributeType.STRING),
      removal_policy=core.RemovalPolicy.RETAIN,
      point_in_time_recovery=True
    )
    db.add_global_secondary_index(
      index_name='fcst_horizon_aware_index',
      partition_key=ddb.Attribute(name='usgs_site#type', type=ddb.AttributeType.STRING),
      sort_key=ddb.Attribute(name='horizon#timestamp', type=ddb.AttributeType.STRING)
    )

    # s3 buckets
    jumpstart_bucket = s3.Bucket(self, id='jumpstart-bucket')
    archive_bucket = s3.Bucket(self, id='archive-bucket')
    model_bucket = s3.Bucket(self, id='model-bucket')

    # * lambda

    env = {
      'WEATHER_KEY': environ['WEATHER_KEY'],
      'RDS_PASS': environ['RDS_PASS'],
      'RDS_HOST': environ['RDS_HOST'],
      'NCEI_HOST': environ['NCEI_HOST'],
      'NCEI_EMAIL': environ['NCEI_EMAIL'],
      'VISUAL_CROSSING_API_KEY': environ['VISUAL_CROSSING_API_KEY'],
      'DATA_TABLE_ARN': db.table_arn,
      'JUMPSTART_BUCKET_NAME': jumpstart_bucket.bucket_name,
      'ARCHIVE_BUCKET_NAME': archive_bucket.bucket_name,
      'MODEL_BUCKET_NAME': model_bucket.bucket_name
    }

    shared_lambda_image = ecr_assets.DockerImageAsset(self, 'shared_lambda_image',
      directory=path.join(path.dirname(__file__), '../../backend'),
      platform=ecr_assets.Platform.LINUX_AMD64
    )

    update = aws_lambda.DockerImageFunction(self, 'update_function',
      code=aws_lambda.DockerImageCode.from_ecr(
        repository=shared_lambda_image.repository,
        tag_or_digest=shared_lambda_image.image_tag,
        entrypoint=['python', '-m', 'awslambdaric'],
        cmd=['index.handle_update'],
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=core.Duration.minutes(5),
      memory_size=1024,
      log_retention=DEFAULT_LOG_RETENTION
    )
    forecast = aws_lambda.DockerImageFunction(self, 'forecast_function',
      code=aws_lambda.DockerImageCode.from_ecr(
        repository=shared_lambda_image.repository,
        tag_or_digest=shared_lambda_image.image_tag,
        entrypoint=['python', '-m', 'awslambdaric'],
        cmd=['index.handle_forecast'],
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=core.Duration.minutes(5),
      memory_size=2048,
      log_retention=DEFAULT_LOG_RETENTION
    )
    access = aws_lambda.DockerImageFunction(self, 'access_function',
      code=aws_lambda.DockerImageCode.from_ecr(
        repository=shared_lambda_image.repository,
        tag_or_digest=shared_lambda_image.image_tag,
        entrypoint=['python', '-m', 'awslambdaric'],
        cmd=['index.handle_access']
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=core.Duration.seconds(30),
      memory_size=1024,
      log_retention=DEFAULT_LOG_RETENTION
    )

    # * fargate

    train_vpc = aws_ec2.Vpc(self, 'flowcast-vpc',
      nat_gateways=0,
      enable_dns_hostnames=True,
      enable_dns_support=True)
    for idx, service in enumerate([
      aws_ec2.InterfaceVpcEndpointAwsService.ECR,
      aws_ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
      aws_ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
      aws_ec2.InterfaceVpcEndpointAwsService.EC2
    ]):
      train_vpc.add_interface_endpoint(f'flowcast-vpc-{idx}-endpoint', 
        service=service,
        private_dns_enabled=True,
        subnets=aws_ec2.SubnetSelection(subnet_type=aws_ec2.SubnetType.PRIVATE_ISOLATED))
    train_vpc.add_gateway_endpoint('flowcast-vpc-s3-endpoint',
      service=aws_ec2.GatewayVpcEndpointAwsService.S3,
      subnets=[aws_ec2.SubnetSelection(subnet_type=aws_ec2.SubnetType.PRIVATE_ISOLATED)])

    train_compute_env = aws_batch.FargateComputeEnvironment(self, 'flowcast-batch-fargate-spot-environment',
      spot=True,
      maxv_cpus=16,
      vpc=train_vpc
    )

    train_job_queue = aws_batch.JobQueue(self, 'flowcast-batch-job-queue',
      compute_environments=[aws_batch.OrderedComputeEnvironment(
        compute_environment=train_compute_env,
        order=1,
      )]
    )

    train_logs = aws_logs.LogGroup(self, 'flowcast-train-loggroup',
      log_group_name='flowcast-train-fargate-loggroup',
      retention=DEFAULT_LOG_RETENTION,
      removal_policy=core.RemovalPolicy.DESTROY
    )

    train_role = aws_iam.Role(self, 'flowcast-train-role',
      assumed_by=aws_iam.ServicePrincipal('ecs-tasks.amazonaws.com'))

    train_job_definition = aws_batch.EcsJobDefinition(self, 'flowcast-batch-job-def',
      container=aws_batch.EcsFargateContainerDefinition(self, 'flowcast-batch-job-container-def',
        image=aws_ecs.ContainerImage.from_ecr_repository(
          repository=shared_lambda_image.repository,
          tag=shared_lambda_image.image_tag
        ),
        command=['python', '-c', 'import sys; from index import handle_train; handle_train(sys.argv[1])', 'Ref::usgs_site'],
        memory=core.Size.gibibytes(32),
        cpu=16,
        environment=env,
        logging=aws_ecs.LogDrivers.aws_logs(
          stream_prefix='ecs',
          log_group=train_logs
        ),
        job_role=train_role
      )
    )
    train_job_definition.container.execution_role.add_to_policy(aws_iam.PolicyStatement(
      effect=aws_iam.Effect.ALLOW,
      actions=[
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage"
      ],
      resources=["*"]))

    # * permissions

    for function in [update, forecast, access]:
      db.grant_full_access(function)
    jumpstart_bucket.grant_read_write(update)
    archive_bucket.grant_read_write(train_role)
    model_bucket.grant_read_write(train_role)
    model_bucket.grant_read_write(forecast)

    # * sfn

    update_task = sfn_tasks.LambdaInvoke(self, 'update_task', lambda_function=update)
    wait = sfn.Wait(self, 'wait', time=sfn.WaitTime.duration(core.Duration.minutes(1)))
    forecast_task = sfn_tasks.LambdaInvoke(self, 'forecast_task', lambda_function=forecast)
    fail_condition = sfn.Condition.not_(sfn.Condition.number_equals('$.Payload.statusCode', 200))
    update_and_forecast_sfn = sfn.StateMachine(self, 'update_and_forecast',
      definition_body=sfn.DefinitionBody.from_chainable(sfn.Chain
        .start(update_task)
        .next(sfn.Choice(self, 'verify_update')
          .when(fail_condition, sfn.Fail(self, 'update_failed'))
          .otherwise(sfn.Pass(self, 'update_successful'))
          .afterwards())
        .next(wait) # allow time for ddb secondary index to sync
        .next(forecast_task)
        .next(sfn.Choice(self, 'verify_forecast')
          .when(fail_condition, sfn.Fail(self, 'forecast_failed'))
          .otherwise(sfn.Pass(self, 'forecast_successful'))
          .afterwards())
        .next(sfn.Succeed(self, 'update_and_forecast_successful'))),
      timeout=core.Duration.minutes(10)
    )

    export = aws_lambda.Function(self, 'export_function',
      code=aws_lambda.AssetCode.from_asset(
        path.join(path.dirname(__file__), '../../backend/src/handlers/export')
      ),
      environment=env,
      handler='export.handler',
      runtime=aws_lambda.Runtime.PYTHON_3_10,
      architecture=aws_lambda.Architecture.ARM_64,
      timeout=core.Duration.seconds(30),
      memory_size=768
    )
    db.grant_full_access(export)
    archive_bucket.grant_read_write(export)

    # * public access url
    core.CfnResource(
      scope=self,
      id='public_access_url',
      type='AWS::Lambda::Url',
      properties={
        'TargetFunctionArn': access.function_arn,
        'AuthType': 'NONE',
        'Cors': {
          'AllowOrigins': ['*'],
          'AllowMethods': ['GET'],
          'MaxAge': 3600
        },
      }
    )
    core.CfnResource(
      scope=self,
      id='public_access_url_permission',
      type='AWS::Lambda::Permission',
      properties={
        'FunctionName': access.function_name,
        'Principal': '*',
        'Action': 'lambda:InvokeFunctionUrl',
        'FunctionUrlAuthType': 'NONE'
      }
    )

    hosted_zone = aws_route53.HostedZone.from_lookup(self, 'hosted-zone',
      domain_name=DOMAIN_NAME
    )
    certificate = aws_certificatemanager.Certificate(self, 'api-certificate',
      domain_name='api.' + DOMAIN_NAME,
      validation=aws_certificatemanager.CertificateValidation.from_dns(hosted_zone)
    )
    public_api = aws_apigateway.LambdaRestApi(self, 'public-api',
      handler=access,
      domain_name=aws_apigateway.DomainNameOptions(
        domain_name='api.' + DOMAIN_NAME,
        certificate=certificate,
        endpoint_type=aws_apigateway.EndpointType.REGIONAL
      )
    )
    aws_route53.ARecord(self, 'api-dns-record',
      zone=hosted_zone,
      record_name='api',
      target=aws_route53.RecordTarget.from_alias(aws_route53_targets.ApiGateway(public_api))
    )

    # * cron
    # 5 min past the hour to give some buffer for providers to update on the hour
    hourly = events.Rule(self, 'hourly', schedule=events.Schedule.expression('cron(5 * * * ? *)'))
    hourly.add_target(event_targets.SfnStateMachine(update_and_forecast_sfn,
      input=events.RuleTargetInput.from_object({ 'usgs_site': '01427510' })))
    weekly = events.Rule(self, 'weekly', schedule=events.Schedule.expression('cron(0 0 ? * SUN *)'))
    weekly.add_target(event_targets.LambdaFunction(export))

    # * client

    source_code_provider = aws_amplify.GitHubSourceCodeProvider(
      owner='jaismith',
      repository='flowcast',
      oauth_token=core.SecretValue.secrets_manager('github-token')
    )

    amplify_role = aws_iam.Role(self, 'amplify-role',
      assumed_by=aws_iam.ServicePrincipal("amplify.amazonaws.com"),
      managed_policies=[
        aws_iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess-Amplify")
      ]
    )

    client_app = aws_amplify.App(self, 'client-app',
      app_name='client',
      platform=aws_amplify.Platform.WEB_COMPUTE,
      environment_variables={
        '_CUSTOM_IMAGE': 'amplify:al2023',
        '_LIVE_UPDATES': '[{"pkg":"@aws-amplify/cli","type":"npm","version":"latest"}]',
        'AMPLIFY_DIFF_DEPLOY': 'false',
        'AMPLIFY_MONOREPO_APP_ROOT': 'client'
      },
      source_code_provider=source_code_provider,
      role=amplify_role,
      build_spec=aws_codebuild.BuildSpec.from_object({
        'version': '1.0',
        'applications': [
          {
            'appRoot': 'client',
            'frontend': {
              'phases': {
                'preBuild': {
                  'commands': [
                    'yarn install'
                  ]
                },
                'build': {
                  'commands': [
                    'yarn build'
                  ]
                }
              },
              'artifacts': {
                'baseDirectory': '.next', # Set the correct base directory
                'files': ['**/*']
              },
              'cache': {
                'paths': [
                  'node_modules/**/*', # Update cache path
                  'yarn.lock' # Cache yarn.lock file
                ]
              }
            }
          }
        ]
      })
    )

    main_branch = client_app.add_branch('main-branch',
      branch_name='main',
      auto_build=True,
      stage='PRODUCTION',
      # asset=client_asset
    )

    custom_domain = client_app.add_domain('custom-domain',
      domain_name=DOMAIN_NAME,
      sub_domains=[
        aws_amplify.SubDomain(branch=main_branch, prefix=''),
        aws_amplify.SubDomain(branch=main_branch, prefix='www')
      ]
    )
