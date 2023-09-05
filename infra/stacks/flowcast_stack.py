from aws_cdk import (
  Duration,
  RemovalPolicy,
  Stack,
  CfnResource,
  aws_dynamodb as ddb,
  aws_lambda,
  aws_events as events,
  aws_events_targets as event_targets,
  aws_ecr as ecr,
  aws_ecr_assets as ecr_assets,
  aws_route53 as route53,
  aws_route53_targets as route53_targets,
  aws_s3 as s3,
  aws_s3_deployment as s3_deployment,
  aws_certificatemanager as certificatemanager,
  aws_cloudfront as cloudfront
)
from constructs import Construct
from os import path, environ
from dotenv import load_dotenv
load_dotenv()

env = {
  'WEATHER_KEY': environ['WEATHER_KEY'],
  'RDS_PASS': environ['RDS_PASS'],
  'RDS_HOST': environ['RDS_HOST'],
  'NCEI_HOST': environ['NCEI_HOST'],
  'NCEI_EMAIL': environ['NCEI_EMAIL'],
  'VISUAL_CROSSING_API_KEY': environ['VISUAL_CROSSING_API_KEY']
}

DOMAIN_NAME = 'flowcast.jaismith.dev'
WEB_APP_DOMAIN = DOMAIN_NAME

class FlowcastStack(Stack):
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
      removal_policy=RemovalPolicy.RETAIN
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
      timeout=Duration.minutes(5),
      memory_size=1024,
    )
    retrain = aws_lambda.DockerImageFunction(self, 'retrain_function',
      code=aws_lambda.DockerImageCode.from_ecr(
        repository=shared_lambda_image.repository,
        tag_or_digest=shared_lambda_image.image_tag,
        cmd=['retrain'],
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(15),
      memory_size=10240
    )
    forecast = aws_lambda.DockerImageFunction(self, 'forecast_function',
      code=aws_lambda.DockerImageCode.from_ecr(
        repository=shared_lambda_image.repository,
        tag_or_digest=shared_lambda_image.image_tag,
        cmd=['forecast'],
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(5),
      memory_size=1024
    )
    access = aws_lambda.DockerImageFunction(self, 'access_function',
      code=aws_lambda.DockerImageCode.from_ecr(
        repository=shared_lambda_image.repository,
        tag_or_digest=shared_lambda_image.image_tag,
        cmd=['access'],
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(5),
      memory_size=768
    )
    for function in [update, retrain, forecast, access]:
      db.grant_full_access(function)
    jumpstart_bucket.grant_read_write(update)
    archive_bucket.grant_read_write(retrain)
    model_bucket.grant_read_write(retrain)
    model_bucket.grant_read_write(forecast)

    export = aws_lambda.Function(self, 'export_function',
      code=aws_lambda.AssetCode.from_asset(
        path.join(path.dirname(__file__), '../../backend/src/handlers/export')
      ),
      environment={
        **env,
        'DATA_TABLE_ARN': db.table_arn,
        'ARCHIVE_BUCKET_NAME': archive_bucket.bucket_name
      },
      handler='export.handler',
      runtime=aws_lambda.Runtime.PYTHON_3_10,
      architecture=aws_lambda.Architecture.ARM_64,
      timeout=Duration.seconds(30),
      memory_size=768
    )
    db.grant_full_access(export)
    archive_bucket.grant_read_write(export)

    # * public access url
    CfnResource(
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
    CfnResource(
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
    hourly.add_target(event_targets.LambdaFunction(update))
    # hourly.add_target(event_targets.LambdaFunction(forecast))

    # daily = events.Rule(self, 'daily', schedule=events.Schedule.expression('cron(0 0 * * ? *)'))
    # daily.add_target(event_targets.LambdaFunction(retrain))

    weekly = events.Rule(self, 'weekly', schedule=events.Schedule.expression('cron(0 0 ? * SUN *)'))
    weekly.add_target(event_targets.LambdaFunction(export))

    # * client

    # get hosted zone
    zone = route53.HostedZone.from_lookup(
      scope=self,
      id='zone',
      domain_name=DOMAIN_NAME
    )

    # create s3 bucket
    site_bucket = s3.Bucket(
      scope=self,
      id='site_bucket',
      bucket_name=WEB_APP_DOMAIN,
      website_index_document='index.html',
      public_read_access=True,
      removal_policy=RemovalPolicy.DESTROY,
      block_public_access=s3.BlockPublicAccess.BLOCK_ACLS,
      access_control=s3.BucketAccessControl.BUCKET_OWNER_FULL_CONTROL
    )

    # generate site cert
    site_certificate = certificatemanager.Certificate(
      scope=self,
      id='site_certificate',
      domain_name=DOMAIN_NAME,
      subject_alternative_names=[f'*.{DOMAIN_NAME}'],
      validation=certificatemanager.CertificateValidation.from_dns(zone)
    )

    site_distribution = cloudfront.CloudFrontWebDistribution(
      scope=self,
      id='site_distribution',
      viewer_certificate=cloudfront.ViewerCertificate.from_acm_certificate(
        certificate=site_certificate,
        aliases=[WEB_APP_DOMAIN],
        security_policy=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        ssl_method=cloudfront.SSLMethod.SNI
      ),
      origin_configs=[cloudfront.SourceConfiguration(
        custom_origin_source=cloudfront.CustomOriginConfig(
          domain_name=site_bucket.bucket_website_domain_name,
          origin_protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY
        ),
        behaviors=[cloudfront.Behavior(is_default_behavior=True)]
      )]
    )

    route53.ARecord(
      scope=self,
      id='site_record',
      record_name=WEB_APP_DOMAIN,
      target=route53.RecordTarget.from_alias(route53_targets.CloudFrontTarget(site_distribution)),
      zone=zone
    )

    s3_deployment.BucketDeployment(
      scope=self,
      id='deployment',
      sources=[s3_deployment.Source.asset(path.join(path.dirname(__file__), '../../client/build'))],
      destination_bucket=site_bucket,
      distribution=site_distribution,
      distribution_paths=['/*'],
      access_control=s3.BucketAccessControl.BUCKET_OWNER_FULL_CONTROL
    )
