#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { FlowcastStack } from '../lib/flowcast';

const app = new cdk.App();
new FlowcastStack(app, 'flowcast-stack', {
  env: { account: '257129854363', region: 'us-east-1' },
});
