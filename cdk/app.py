# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""CDK app entry for the Tokenomics stack.

Region-parameterized and self-contained: every resource is created by this stack with
stack/region-prefixed names, so it has no dependency on any pre-existing resources and can
be deployed to any region (subject to bedrock-mantle availability).

Deploy:
    cdk deploy --context account=<ACCOUNT_ID> --context region=us-east-2
"""
import os

import aws_cdk as cdk

from cdk.stacks.tokenomics_stack import TokenomicsStack

app = cdk.App()
config = app.node.try_get_context("config") or {}

account = (
    app.node.try_get_context("account")
    or config.get("account")
    or os.environ.get("CDK_DEFAULT_ACCOUNT")
)
region = (
    app.node.try_get_context("region")
    or config.get("region")
    or os.environ.get("CDK_DEFAULT_REGION")
    or "us-east-2"
)

TokenomicsStack(
    app,
    "TokenomicsStack",
    env=cdk.Environment(account=account, region=region),
    config=config,
)

app.synth()
