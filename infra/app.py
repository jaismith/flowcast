#!/usr/bin/env python3
import os

import aws_cdk as cdk

from cloud.flowcast_stack import FlowcastStack

app = cdk.App()
FlowcastStack(app, "flowcast-stack",
    # use personal aws
    env=cdk.Environment(account='257129854363', region='us-east-1'),
)

app.synth()
