from aws_cdk import (
    Duration,
    Stack,
    aws_rds as rds,
    aws_ec2 as ec2,
    aws_lambda,
    aws_events as events,
    aws_events_targets as targets
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
        vpc = ec2.Vpc(self, 'flowcast-vpc')

        # * db
        db = rds.DatabaseInstance(
            self, 'flowcast-rds',
            instance_identifier='flowcast-data',
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_12_10),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
            allocated_storage=5,
            max_allocated_storage=20,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC
            ),
            publicly_accessible=True
        )
        db.connections.allow_default_port_from_any_ipv4('allow public psql access')

        # * lambda
        update = aws_lambda.DockerImageFunction(self, 'update_function',
            code=aws_lambda.DockerImageCode.from_image_asset(path.join(path.dirname(__file__), '../../src'), cmd=['index.handle_update']),
            environment=env,
            architecture=aws_lambda.Architecture.X86_64,
            timeout=Duration.minutes(5),
            memory_size=512
        )
        retrain = aws_lambda.DockerImageFunction(self, 'retrain_function',
            code=aws_lambda.DockerImageCode.from_image_asset(path.join(path.dirname(__file__), '../../src'), cmd=['index.handle_retrain']),
            environment=env,
            architecture=aws_lambda.Architecture.X86_64,
            timeout=Duration.minutes(10),
            memory_size=4096
        )

        # * cron
        rule = events.Rule(self, 'cron_rule', schedule=events.Schedule.expression('cron(0 * * * ? *)'))
        rule.add_target(targets.LambdaFunction(update))
        rule.add_target(targets.LambdaFunction(retrain))
