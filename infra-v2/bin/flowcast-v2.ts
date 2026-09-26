#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";

import { FlowcastV2Stack } from "../lib/flowcast-v2-stack";

const app = new cdk.App();

// Beside the legacy `flowcast-stack` and the `flowcast-archiver` stack; deployable and removable on its own.
new FlowcastV2Stack(app, "flowcast-v2", {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
  alertEmail: app.node.tryGetContext("alertEmail"),
});

cdk.Tags.of(app).add("project", "flowcast");
cdk.Tags.of(app).add("component", "v2");
