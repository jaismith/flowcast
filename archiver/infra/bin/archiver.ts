#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";

import { ArchiverStack } from "../lib/archiver-stack";

const app = new cdk.App();

// Deliberately separate from the legacy `flowcast-stack` so it can be deployed and torn down on its own.
new ArchiverStack(app, "flowcast-archiver", {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
  alertEmail: app.node.tryGetContext("alertEmail"),
});

cdk.Tags.of(app).add("project", "flowcast");
cdk.Tags.of(app).add("component", "archiver");
