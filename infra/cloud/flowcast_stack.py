from aws_cdk import (
  Duration,
  Stack,
  CfnResource,
  aws_rds as rds,
  aws_ec2 as ec2,
  aws_lambda,
  aws_events as events,
  aws_events_targets as targets,
  aws_ecr_assets as ecr_assets,
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
    image = aws_lambda.DockerImageCode.from_image_asset(
      path.join(path.dirname(__file__), '../../src'),
      cmd=['index.handle_update'],
      platform=ecr_assets.Platform.LINUX_AMD64
    )
    update = aws_lambda.DockerImageFunction(self, 'update_function',
      code=image,
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(5),
      memory_size=512
    )
    retrain = aws_lambda.DockerImageFunction(self, 'retrain_function',
      code=image,
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(15),
      memory_size=10240
    )
    forecast = aws_lambda.DockerImageFunction(self, 'forecast_function',
      code=image,
      environment=env,
      architecture=aws_lambda.Architecture.X86_64,
      timeout=Duration.minutes(5),
      memory_size=512
    )
    access = aws_lambda.DockerImageFunction(self, 'access_function',
      code=image,
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
        'Cors': { 'AllowOrigins': ['*'] }
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
    hourly.add_target(targets.LambdaFunction(update))
    hourly.add_target(targets.LambdaFunction(forecast))

    daily = events.Rule(self, 'daily', schedule=events.Schedule.expression('cron(0 0 * * ? *)'))
    daily.add_target(targets.LambdaFunction(retrain))
