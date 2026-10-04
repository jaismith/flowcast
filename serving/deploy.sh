#!/usr/bin/env bash
# Deploy the production serving backend (prod only): the `flowcast-serve` CDK stack.
# Needs Node 22, uv, and Docker able to build linux/arm64 images (QEMU binfmt on x86 hosts). Outputs (bucket and
# distribution names for the page's deploy script) go to serving/.deploy-outputs.json.
set -euo pipefail
cd "$(dirname "$0")/../infra-v2"
npm ci --silent
npx cdk deploy flowcast-serve --require-approval never --outputs-file ../serving/.deploy-outputs.json "$@"
