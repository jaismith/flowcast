import aws_cdk as core
from aws_cdk import (
  aws_dynamodb as ddb,
  aws_lambda,
  aws_events as events,
  aws_events_targets as event_targets,
  aws_ecr_assets as ecr_assets,
  aws_route53 as route53,
  aws_route53_targets as route53_targets,
  aws_s3 as s3,
  aws_s3_deployment as s3_deployment,
  aws_s3_assets as s3_assets,
  aws_certificatemanager as certificatemanager,
  aws_cloudfront as cloudfront,
  aws_cloudfront_origins as cloudfront_origins,
  aws_logs,
  aws_stepfunctions as sfn,
  aws_stepfunctions_tasks as sfn_tasks,
  aws_apigateway,
  aws_amplify_alpha as aws_amplify,
  aws_codebuild
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

    # * cron
    hourly = events.Rule(self, 'hourly', schedule=events.Schedule.expression('cron(0 * * * ? *)'))
    hourly.add_target(event_targets.SfnStateMachine(update_and_forecast_sfn))
    weekly = events.Rule(self, 'weekly', schedule=events.Schedule.expression('cron(0 0 ? * SUN *)'))
    weekly.add_target(event_targets.LambdaFunction(export))
    weekly.add_target(event_targets.LambdaFunction(retrain))

    # * client

    # lambda_adapter_layer = aws_lambda.LayerVersion.from_layer_version_arn(self, 'lambda_adapter_layer',
    #   layer_version_arn=f'arn:aws:lambda:{self.region}:753240598075:layer:LambdaAdapterLayerArm64:17'
    # )

    # next_lambda = aws_lambda.Function(self, 'next_lambda',
    #   runtime=aws_lambda.Runtime.NODEJS_20_X,
    #   handler='run.sh',
    #   code=aws_lambda.Code.from_asset(path.join(
    #     path.dirname(__file__),
    #     '../../client/.next/standalone'
    #   )),
    #   architecture=aws_lambda.Architecture.ARM_64,
    #   environment={
    #     'AWS_LAMBDA_EXEC_WRAPPER': '/opt/bootstrap',
    #     'AWS_LWA_ENABLE_COMPRESSION': 'true',
    #     'RUST_LOG': 'debug',
    #     'PORT': '8080'
    #   },
    #   layers=[lambda_adapter_layer]
    # )

    # api = aws_apigateway.RestApi(self, 'next_api',
    #   default_cors_preflight_options=aws_apigateway.CorsOptions(
    #     allow_origins=aws_apigateway.Cors.ALL_ORIGINS,
    #     allow_methods=aws_apigateway.Cors.ALL_METHODS
    #   )
    # )

    # next_lambda_integration = aws_apigateway.LambdaIntegration(
    #   handler=next_lambda,
    #   allow_test_invoke=False
    # )
    # api.root.add_method('ANY', next_lambda_integration)
    # api.root.add_proxy(
    #   default_integration=aws_apigateway.LambdaIntegration(
    #     handler=next_lambda,
    #     allow_test_invoke=False
    #   ),
    #   any_method=True
    # )

    # next_logging_bucket = s3.Bucket(self, 'next_logging_bucket',
    #   block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
    #   encryption=s3.BucketEncryption.S3_MANAGED,
    #   versioned=True,
    #   access_control=s3.BucketAccessControl.LOG_DELIVERY_WRITE
    # )

    # next_bucket = s3.Bucket(self, 'next_bucket',
    #   block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
    #   encryption=s3.BucketEncryption.S3_MANAGED,
    #   versioned=True,
    #   server_access_logs_bucket=next_logging_bucket,
    #   server_access_logs_prefix='s3-access-logs'
    # )

    # core.CfnOutput(self, 'next-bucket',
    #   value=next_bucket.bucket_name
    # )

    # route53_zone = route53.HostedZone.from_lookup(
    #   scope=self,
    #   id='zone',
    #   domain_name=DOMAIN_NAME
    # )

    # site_certificate = certificatemanager.Certificate(
    #   scope=self,
    #   id='site_certificate',
    #   domain_name=DOMAIN_NAME,
    #   subject_alternative_names=[f'*.{DOMAIN_NAME}'],
    #   validation=certificatemanager.CertificateValidation.from_dns(route53_zone)
    # )

    # cloudfront_distribution = cloudfront.Distribution(self, 'distribution',
    #   default_behavior=cloudfront.BehaviorOptions(
    #     origin=cloudfront_origins.RestApiOrigin(api),
    #     viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    #     cache_policy=cloudfront.CachePolicy.CACHING_DISABLED
    #   ),
    #   additional_behaviors={
    #     '_next/static/*': cloudfront.BehaviorOptions(
    #       origin=cloudfront_origins.S3Origin(next_bucket),
    #       viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY
    #     ),
    #     'static/*': cloudfront.BehaviorOptions(
    #       origin=cloudfront_origins.S3Origin(next_bucket),
    #       viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY
    #     )
    #   },
    #   minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2018,
    #   log_bucket=next_logging_bucket,
    #   log_file_prefix='cloudfront-access-logs',
    #   certificate=site_certificate,
    #   domain_names=[WEB_APP_DOMAIN]
    # )

    # route53.ARecord(
    #   scope=self,
    #   id='site_record',
    #   record_name=WEB_APP_DOMAIN,
    #   target=route53.RecordTarget.from_alias(route53_targets.CloudFrontTarget(cloudfront_distribution)),
    #   zone=route53_zone
    # )

    # core.CfnOutput(self, 'cloudfront-url',
    #   value=f'https://{cloudfront_distribution.distribution_domain_name}'
    # )

    # s3_deployment.BucketDeployment(self, 'next-static-bucket-deployment',
    #   sources=[s3_deployment.Source.asset(path.join(
    #     path.dirname(__file__),
    #     '../../client/.next/static/'
    #   ))],
    #   destination_bucket=next_bucket,
    #   destination_key_prefix='_next/static',
    #   distribution=cloudfront_distribution,
    #   distribution_paths=['/_next/static/*']
    # )

    # s3_deployment.BucketDeployment(self, 'next-public-bucket-deployment',
    #   sources=[s3_deployment.Source.asset(path.join(
    #     path.dirname(__file__),
    #     '../../client/public/static'
    #   ))],
    #   destination_bucket=next_bucket,
    #   destination_key_prefix='static',
    #   distribution=cloudfront_distribution,
    #   distribution_paths=['/static/*']
    # )

    # client_asset = s3_assets.Asset(self, 'client-asset',
    #   path=path.join(
    #     path.dirname(__file__),
    #     '../../client'
    #   )
    # )

    source_code_provider = aws_amplify.GitHubSourceCodeProvider(
      owner='jaismith',
      repository='flowcast',
      oauth_token=core.SecretValue.secrets_manager('github-token')
    )

    client_app = aws_amplify.App(self, 'client-app',
      app_name='client',
      platform=aws_amplify.Platform.WEB_COMPUTE,
      environment_variables={
        '_CUSTOM_IMAGE': 'amplify:al2023',
        'AMPLIFY_MONOREPO_APP_ROOT': 'client'
      },
      source_code_provider=source_code_provider,
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
      auto_build=True
    )
    # client_app.add_branch('prod', asset=client_asset)

    route53_zone = route53.HostedZone.from_lookup(
      scope=self,
      id='zone',
      domain_name=DOMAIN_NAME
    )

    # site_certificate = certificatemanager.Certificate(
    #   scope=self,
    #   id='site_certificate',
    #   domain_name=DOMAIN_NAME,
    #   subject_alternative_names=[f'*.{DOMAIN_NAME}'],
    #   validation=certificatemanager.CertificateValidation.from_dns(route53_zone)
    # )

    custom_domain = client_app.add_domain('custom-domain',
      domain_name=DOMAIN_NAME,
      sub_domains=[
        aws_amplify.SubDomain(branch=main_branch),
        aws_amplify.SubDomain(branch=main_branch, prefix='www')
      ]
    )
