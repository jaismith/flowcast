from aws_cdk import (
  Duration,
  RemovalPolicy,
  Stack,
  CfnResource,
  aws_rds as rds,
  aws_ec2 as ec2,
  aws_lambda,
  aws_events as events,
  aws_events_targets as event_targets,
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
  'NCEI_EMAIL': environ['NCEI_EMAIL']
}

DOMAIN_NAME = 'flowcast.jaismith.dev'
WEB_APP_DOMAIN = f'app.{DOMAIN_NAME}'

class FlowcastStack(Stack):
  def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
    super().__init__(scope, construct_id, **kwargs)

    # * vpc
    vpc = ec2.Vpc(self, 'flowcast-vpc',
      vpn_gateway=False,
      nat_gateways=0,
      subnet_configuration=[
        ec2.SubnetConfiguration(
          cidr_mask=23,
          name='Public',
          subnet_type=ec2.SubnetType.PUBLIC
        ),
        ec2.SubnetConfiguration(
          cidr_mask=23,
          name='Isolated',
          subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
        )
      ]
    )

    # * db
    db = rds.DatabaseInstance(
      self, 'flowcast-rds',
      instance_identifier='flowcast-data',
      engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_12_10),
      instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
      allocated_storage=5,
      max_allocated_storage=20,
      vpc=vpc,
      vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
      publicly_accessible=True,
      delete_automated_backups=True
    )
    db.connections.allow_default_port_from_any_ipv4('allow public psql access')

    # * lambda
    update = aws_lambda.DockerImageFunction(self, 'update_function',
      code=aws_lambda.DockerImageCode.from_image_asset(
        path.join(path.dirname(__file__), '../../src/backend'),
        cmd=['index.handle_update'],
        platform=ecr_assets.Platform.LINUX_AMD64
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(5),
      memory_size=512
    )
    retrain = aws_lambda.DockerImageFunction(self, 'retrain_function',
      code=aws_lambda.DockerImageCode.from_image_asset(
        path.join(path.dirname(__file__), '../../src/backend'),
        cmd=['index.handle_retrain'],
        platform=ecr_assets.Platform.LINUX_AMD64
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(15),
      memory_size=10240
    )
    forecast = aws_lambda.DockerImageFunction(self, 'forecast_function',
      code=aws_lambda.DockerImageCode.from_image_asset(
        path.join(path.dirname(__file__), '../../src/backend'),
        cmd=['index.handle_forecast'],
        platform=ecr_assets.Platform.LINUX_AMD64
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(5),
      memory_size=512
    )
    access = aws_lambda.DockerImageFunction(self, 'access_function',
      code=aws_lambda.DockerImageCode.from_image_asset(
        path.join(path.dirname(__file__), '../../src/backend'),
        cmd=['index.handle_access'],
        platform=ecr_assets.Platform.LINUX_AMD64
      ),
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(5),
      memory_size=256
    )

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
    hourly.add_target(event_targets.LambdaFunction(forecast))

    daily = events.Rule(self, 'daily', schedule=events.Schedule.expression('cron(0 0 * * ? *)'))
    daily.add_target(event_targets.LambdaFunction(retrain))

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
      removal_policy=RemovalPolicy.DESTROY
    )

    site_certificate = certificatemanager.DnsValidatedCertificate(
      scope=self,
      id='site_certificate',
      domain_name=DOMAIN_NAME,
      hosted_zone=zone,
      region='us-east-1'
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
      sources=[s3_deployment.Source.asset(path.join(path.dirname(__file__), '../../src/client/build'))],
      destination_bucket=site_bucket,
      distribution=site_distribution,
      distribution_paths=['/*']
    )
