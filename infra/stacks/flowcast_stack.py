import aws_cdk as core
from aws_cdk import (
  aws_dynamodb as ddb,
  aws_lambda,
  aws_events as events,
  aws_events_targets as event_targets,
  aws_ecr_assets as ecr_assets,
  aws_s3 as s3,
  # aws_s3_assets as s3_assets,
  aws_logs,
  aws_stepfunctions as sfn,
  aws_stepfunctions_tasks as sfn_tasks,
  aws_amplify_alpha as aws_amplify,
  aws_iam,
  aws_codebuild,
  aws_apigateway,
  aws_route53,
  aws_route53_targets,
  aws_certificatemanager
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
      billing_mode=ddb.BillingMode.PROVISIONED,
      write_capacity=5,
      read_capacity=5,
      partition_key=ddb.Attribute(name='usgs_site#type', type=ddb.AttributeType.STRING),
      sort_key=ddb.Attribute(name='timestamp', type=ddb.AttributeType.NUMBER),
      removal_policy=core.RemovalPolicy.RETAIN,
      point_in_time_recovery=True
    )
    # limit to free tier
    read_capacity = db.auto_scale_read_capacity(min_capacity=1, max_capacity=10)
    read_capacity.scale_on_utilization(target_utilization_percent=80)
    write_capacity = db.auto_scale_write_capacity(min_capacity=1, max_capacity=10)
    write_capacity.scale_on_utilization(target_utilization_percent=80)

    db.add_global_secondary_index(
      index_name='fcst_origin_aware_index',
      partition_key=ddb.Attribute(name='usgs_site#type', type=ddb.AttributeType.STRING),
      sort_key=ddb.Attribute(name='origin#timestamp', type=ddb.AttributeType.STRING),
      read_capacity=5,
      write_capacity=5
    )
    gsi_read_capacity = db.auto_scale_global_secondary_index_read_capacity(
      'fcst_origin_aware_index',
      min_capacity=1,
      max_capacity=10
    )
    gsi_read_capacity.scale_on_utilization(target_utilization_percent=80)
    gsi_write_capacity = db.auto_scale_global_secondary_index_write_capacity(
      'fcst_origin_aware_index',
      min_capacity=1,
      max_capacity=10
    )
    gsi_write_capacity.scale_on_utilization(target_utilization_percent=80)

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
        cmd=['index.handle_update'],
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=core.Duration.minutes(5),
      memory_size=1024,
      log_retention=DEFAULT_LOG_RETENTION
    )
    retrain = aws_lambda.DockerImageFunction(self, 'retrain_function',
      code=aws_lambda.DockerImageCode.from_ecr(
        repository=shared_lambda_image.repository,
        tag_or_digest=shared_lambda_image.image_tag,
        cmd=['index.handle_retrain'],
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=core.Duration.minutes(15),
      memory_size=10240,
      log_retention=DEFAULT_LOG_RETENTION
    )
    forecast = aws_lambda.DockerImageFunction(self, 'forecast_function',
      code=aws_lambda.DockerImageCode.from_ecr(
        repository=shared_lambda_image.repository,
        tag_or_digest=shared_lambda_image.image_tag,
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
        cmd=['index.handle_access'],
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=core.Duration.seconds(30),
      memory_size=1024,
      log_retention=DEFAULT_LOG_RETENTION
    )
    for function in [update, retrain, forecast, access]:
      db.grant_full_access(function)
    jumpstart_bucket.grant_read_write(update)
    archive_bucket.grant_read_write(retrain)
    model_bucket.grant_read_write(retrain)
    model_bucket.grant_read_write(forecast)

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
      ),
    )
    aws_route53.ARecord(self, 'api-dns-record',
      zone=hosted_zone,
      record_name='api',
      target=aws_route53.RecordTarget.from_alias(aws_route53_targets.ApiGateway(public_api))
    )

    # * cron
    hourly = events.Rule(self, 'hourly', schedule=events.Schedule.expression('cron(0 * * * ? *)'))
    hourly.add_target(event_targets.SfnStateMachine(update_and_forecast_sfn))
    weekly = events.Rule(self, 'weekly', schedule=events.Schedule.expression('cron(0 0 ? * SUN *)'))
    weekly.add_target(event_targets.LambdaFunction(export))
    weekly.add_target(event_targets.LambdaFunction(retrain))

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

    # client_asset = s3_assets.Asset(self, 'client-asset',
    #   path=path.join(
    #     path.dirname(__file__),
    #     '../../client'
    #   )
    # )

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
