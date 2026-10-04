#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";

import { FlowcastV2Stack } from "../lib/flowcast-v2-stack";
import { FlowcastServeStack } from "../lib/serving";

const app = new cdk.App();

// Beside the legacy `flowcast-stack` and the `flowcast-archiver` stack; deployable and removable on its own.
const v2 = new FlowcastV2Stack(app, "flowcast-v2", {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
  alertEmail: app.node.tryGetContext("alertEmail"),
});

// Production serving: site + data + API behind one CloudFront distribution, forecast runs, lazy wakes.
const serve = new FlowcastServeStack(app, "flowcast-serve", {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
  hostedZoneId: "Z00918051TLG71S4M4AE4",
  alertTopicName: "flowcast-v2-alerts",
});

cdk.Tags.of(app).add("project", "flowcast");
cdk.Tags.of(v2).add("component", "v2");
cdk.Tags.of(serve).add("component", "serving");
