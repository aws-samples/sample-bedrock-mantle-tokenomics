# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tokenomics stack.

Cost governance + progressive model tiering for Bedrock-backed AI coding assistants via
AgentCore Gateway + bedrock-mantle.

Self-contained and region-parameterized. All physical names are prefixed with a
region-derived suffix so the stack has no dependency on pre-existing resources and never
collides with other stacks/tests in the account.

Build phases (this file grows as phases land):
  Phase 1 (this commit): DynamoDB tables + Cognito user pool / Hosted UI / app client.
  Phase 2: request interceptor Lambda.
  Phase 3: AgentCore Gateway + mantle inference target + policy engine + Cedar policy.
  Phase 4: metering (Metric Stream -> Firehose -> enrich Lambda -> S3).
  Phase 5: aggregator + pricing refresh Lambdas (EventBridge 5-min).
  Phase 6-8: backend API + frontends.
"""
import json
from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_dynamodb as dynamodb,
    aws_cognito as cognito,
    aws_lambda as lambda_,
    aws_iam as iam,
    aws_bedrockagentcore as agentcore,
    aws_s3 as s3,
    aws_kinesisfirehose as firehose,
    aws_cloudwatch as cloudwatch,
    aws_events as events,
    aws_events_targets as targets,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_s3_deployment as s3_deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_logs as logs,
    CfnOutput,
    Fn,
)
from constructs import Construct


# Default CloudWatch Logs retention for all Lambda log groups. Without an explicit retention a
# Lambda's log group is created with "never expire", which accrues storage cost indefinitely and
# is flagged by security tooling. One year is a sensible default for an audit/cost-governance
# sample; adjust per your compliance needs.
_LOG_RETENTION = logs.RetentionDays.ONE_YEAR


class TokenomicsStack(Stack):
    def _lambda_log_group(self, construct_id: str, function_name: str) -> logs.LogGroup:
        """Create an explicit, retention-controlled log group for a Lambda so its logs do not
        default to never-expire. The physical name matches the Lambda default
        (/aws/lambda/<function-name>) so the function writes to it; passing it via the function's
        `log_group` prop makes CDK own it (modern replacement for the deprecated `log_retention`)."""
        return logs.LogGroup(
            self, construct_id,
            log_group_name=f"/aws/lambda/{function_name}",
            retention=_LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def __init__(self, scope: Construct, construct_id: str, config: dict | None = None, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        config = config or {}

        # Region-derived name suffix keeps physical names unique per region and avoids
        # collisions with any existing resources in the account.
        suffix = self.region

        # ------------------------------------------------------------------
        # DynamoDB tables
        # ------------------------------------------------------------------

        # Config / mapping table.
        #   PK = sub  (the logged-in developer's stable JWT subject id)
        #   attrs: project_id, developer_email, group, cost_center, daily_budget_usd,
        #          allowed_models (list), tier1_model, tier2_model, tier3_model,
        #          thresholds (list, default [75, 90, 100])
        # The request interceptor reads this by `sub` on each request; the admin console
        # writes it when a developer is assigned to a project.
        self.config_table = dynamodb.Table(
            self, "ConfigTable",
            table_name=f"Tokenomics-Config-{suffix}",
            partition_key=dynamodb.Attribute(name="sub", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        # GSI to let the admin console list all developers assigned to a project.
        self.config_table.add_global_secondary_index(
            index_name="by-project",
            partition_key=dynamodb.Attribute(name="project_id", type=dynamodb.AttributeType.STRING),
        )

        # Budget-state table (overwritten every ~5 min by the aggregator).
        #   PK = project_id
        #   attrs: date, input_tokens, output_tokens, cost_today_usd, budget_pct,
        #          budget_exceeded (bool), models_used, updated_at
        # The interceptor reads this to decide tier / block; the aggregator overwrites it.
        self.budget_table = dynamodb.Table(
            self, "BudgetTable",
            table_name=f"Tokenomics-Budget-{suffix}",
            partition_key=dynamodb.Attribute(name="project_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Pricing cache table.
        #   PK = model_id ; attrs: input_price_per_1k, output_price_per_1k,
        #        price_available (bool), last_updated
        # Populated by the pricing-refresh Lambda (AWS Pricing API); read by the aggregator.
        self.pricing_table = dynamodb.Table(
            self, "PricingTable",
            table_name=f"Tokenomics-Pricing-{suffix}",
            partition_key=dynamodb.Attribute(name="model_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Request-correlation table (cache metering).
        #   PK = request_id ; attrs: project_id, model, expires_at (TTL epoch seconds)
        # For HTTP/inference targets the RESPONSE interceptor cannot see the request
        # (gatewayRequest is null), so the REQUEST interceptor stamps REQUEST_ID -> project+model
        # here and the cache-meter RESPONSE interceptor reads it back to attribute cache tokens
        # per project. Rows are tiny and auto-expire via DynamoDB TTL (~15 min), so the table
        # stays near-empty. PITR not needed (ephemeral correlation data).
        self.reqmap_table = dynamodb.Table(
            self, "ReqMapTable",
            table_name=f"Tokenomics-ReqMap-{suffix}",
            partition_key=dynamodb.Attribute(name="request_id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expires_at",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Cognito - user pool + Hosted UI + app client
        # ------------------------------------------------------------------
        # Developers log in here (Hosted UI). The JWT `sub` identifies the developer and is
        # the mapping key into the config table. Design stays IdP-agnostic: the gateway
        # trusts this pool's discovery URL, but any OIDC IdP (Okta / IAM Identity Center)
        # can be swapped in for production.
        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name=f"Tokenomics-Users-{suffix}",
            self_sign_up_enabled=False,           # admins provision developers
            sign_in_aliases=cognito.SignInAliases(email=True, username=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            feature_plan=cognito.FeaturePlan.PLUS,
            standard_threat_protection_mode=cognito.StandardThreatProtectionMode.FULL_FUNCTION,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Groups (for future group-based model access + CostCenter/Group rollups).
        cognito.CfnUserPoolGroup(
            self, "AdminsGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="admins",
            description="Tokenomics administrators (admin console access).",
        )
        cognito.CfnUserPoolGroup(
            self, "DevelopersGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="developers",
            description="Developers who consume models through the gateway.",
        )

        # Resource server + custom scope required for the gateway's CUSTOM_JWT authorizer.
        gateway_scope = cognito.ResourceServerScope(
            scope_name="invoke", scope_description="Invoke the Tokenomics gateway."
        )
        self.resource_server = self.user_pool.add_resource_server(
            "GatewayResourceServer",
            identifier="tokenomics-gateway",
            scopes=[gateway_scope],
        )

        # App client used by the developer portal + IDEs. Supports the Hosted UI
        # authorization-code flow (user login) so the JWT `sub` is the individual developer.
        self.user_pool_client = self.user_pool.add_client(
            "PortalClient",
            user_pool_client_name=f"Tokenomics-Portal-{suffix}",
            generate_secret=False,   # public client (browser/IDE); PKCE-based auth-code flow
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                    cognito.OAuthScope.resource_server(self.resource_server, gateway_scope),
                ],
                # Callback URLs get finalized once the portal is deployed (Phase 7).
                callback_urls=["http://localhost:3000/callback"],
                logout_urls=["http://localhost:3000/logout"],
            ),
            prevent_user_existence_errors=True,
        )

        # Hosted UI domain (login page). Domain prefix must be globally unique; account+region
        # suffix keeps it collision-free.
        self.user_pool_domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"tokenomics-{self.account}-{suffix}"
            ),
        )

        # ==================================================================
        # Phase 3: request interceptor Lambda + AgentCore Gateway + mantle
        #          inference target + policy engine + static Cedar policy
        # ==================================================================
        oidc_discovery_url = (
            f"https://cognito-idp.{self.region}.amazonaws.com/"
            f"{self.user_pool.user_pool_id}/.well-known/openid-configuration"
        )
        virtual_model = config.get("virtual_model", "team-coding-model")
        target_name = config.get("target_name", "bedrock-models")
        enforce_mode = config.get("enforce_mode", "cedar")  # "cedar" | "interceptor"

        # ---- Request interceptor Lambda ----
        # No AWS managed policy: logging is scoped to this function's own log group only.
        interceptor_role = iam.Role(
            self, "InterceptorRole",
            role_name=f"Tokenomics-Interceptor-{suffix}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        interceptor_role.add_to_policy(iam.PolicyStatement(
            sid="ScopedLambdaLogs",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[
                f"arn:aws:logs:{self.region}:{self.account}:"
                f"log-group:/aws/lambda/Tokenomics-Interceptor-{suffix}:*"
            ],
        ))
        # Least privilege: read-only on exactly the two tables it needs.
        self.config_table.grant_read_data(interceptor_role)
        self.budget_table.grant_read_data(interceptor_role)
        # Write the REQUEST_ID -> project/model correlation row (cache metering). Write-only:
        # the interceptor never reads this table back.
        self.reqmap_table.grant_write_data(interceptor_role)

        self.interceptor_fn = lambda_.Function(
            self, "InterceptorFn",
            function_name=f"Tokenomics-Interceptor-{suffix}",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/gw_interceptor"),
            role=interceptor_role,
            timeout=Duration.seconds(10),
            memory_size=256,
            log_group=self._lambda_log_group(
                "InterceptorLogGroup", f"Tokenomics-Interceptor-{suffix}"),
            environment={
                "CONFIG_TABLE": self.config_table.table_name,
                "BUDGET_TABLE": self.budget_table.table_name,
                "REQMAP_TABLE": self.reqmap_table.table_name,
                "VIRTUAL_MODEL": virtual_model,
                "TARGET_PREFIX": target_name,
                "ENFORCE_MODE": enforce_mode,
                "FAIL_OPEN": "false",
            },
        )
        # Allow the AgentCore gateway service to invoke the interceptor.
        # CONFUSED-DEPUTY GUARD: scope the service-principal grant to THIS account (source_account)
        # and to bedrock-agentcore gateway ARNs in this account/region (source_arn). Without these
        # conditions, any account's bedrock-agentcore service could invoke this function. The
        # gateway resource is created later in this stack, so we scope by ARN pattern (this
        # account's gateways) rather than the single concrete gateway id.
        _agentcore_gw_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
        self.interceptor_fn.add_permission(
            "AllowAgentCoreInvoke",
            principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_account=self.account,
            source_arn=_agentcore_gw_arn,
        )

        # ---- Cache-meter Lambda (RESPONSE interceptor) ----
        # Reads cache tokens from the buffered response `usage` and emits them to the custom
        # Tokenomics/Cache CloudWatch namespace, attributed per project via the REQUEST_ID
        # correlation row. It NEVER transforms the response (always returns a passthrough), so a
        # metering bug can't corrupt model output.
        cache_meter_role = iam.Role(
            self, "CacheMeterRole",
            role_name=f"Tokenomics-CacheMeter-{suffix}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        cache_meter_role.add_to_policy(iam.PolicyStatement(
            sid="ScopedLambdaLogs",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[
                f"arn:aws:logs:{self.region}:{self.account}:"
                f"log-group:/aws/lambda/Tokenomics-CacheMeter-{suffix}:*"
            ],
        ))
        # Read-only on the correlation table (REQUEST_ID -> project/model).
        self.reqmap_table.grant_read_data(cache_meter_role)
        # cloudwatch:PutMetricData has NO resource-level ARN, but it DOES support the
        # cloudwatch:namespace condition key. Scope emission to exactly the custom cache
        # namespace so this role cannot write any other metrics.
        # https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazoncloudwatch.html
        cache_meter_role.add_to_policy(iam.PolicyStatement(
            sid="PutCacheMetricsOnly",
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": "Tokenomics/Cache"}},
        ))
        self.cache_meter_fn = lambda_.Function(
            self, "CacheMeterFn",
            function_name=f"Tokenomics-CacheMeter-{suffix}",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/gw_cache_meter"),
            role=cache_meter_role,
            timeout=Duration.seconds(10),
            memory_size=256,
            log_group=self._lambda_log_group(
                "CacheMeterLogGroup", f"Tokenomics-CacheMeter-{suffix}"),
            environment={
                "REQMAP_TABLE": self.reqmap_table.table_name,
                "CACHE_NAMESPACE": "Tokenomics/Cache",
                "TARGET_PREFIX": target_name,
            },
        )
        # Same confused-deputy guard as the request interceptor (this account's gateways only).
        self.cache_meter_fn.add_permission(
            "AllowAgentCoreInvokeCacheMeter",
            principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_account=self.account,
            source_arn=_agentcore_gw_arn,
        )

        # ---- Gateway execution role ----
        # Needs: evaluate policy engine, invoke the interceptor, and reach bedrock-mantle.
        # Trust the AgentCore service principal, scoped with the AWS-documented confused-deputy
        # conditions (aws:SourceAccount = this account, aws:SourceArn = agentcore ARNs in this
        # account/region). The bare service principal (no conditions) causes gateway creation to
        # fail the synchronous policy-engine check (GetPolicyEngine AccessDenied), because the
        # assume-role context doesn't match what the service presents.
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html
        gateway_role = iam.Role(
            self, "GatewayRole",
            role_name=f"Tokenomics-Gateway-{suffix}",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"
                    },
                },
            ),
        )
        gateway_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[self.interceptor_fn.function_arn, self.cache_meter_fn.function_arn],
        ))
        # bedrock-mantle inference (connector target). Explicit actions, scoped to project
        # resources (matches AWS managed policy AmazonBedrockMantleInferenceAccess resource
        # scoping). ACCEPTED RISK: project/* is intentional -- developer projects are created
        # dynamically at onboarding, so the gateway must reach any project in the account/region.
        # Narrowing to a single project would break the per-developer multi-project design.
        mantle_project_arn = f"arn:aws:bedrock-mantle:{self.region}:{self.account}:project/*"
        gateway_role.add_to_policy(iam.PolicyStatement(
            sid="BedrockMantleInference",
            actions=[
                "bedrock-mantle:CreateInference",
                "bedrock-mantle:GetModel",
                "bedrock-mantle:ListModels",
                "bedrock-mantle:GetProject",
                "bedrock-mantle:ListProjects",
            ],
            resources=[mantle_project_arn],
        ))
        # CallWithBearerToken has NO resource-level ARN support (verified: the AWS managed
        # policy AmazonBedrockMantleInferenceAccess also uses "*" for this exact action).
        # We add a condition so the permission is only usable in the bedrock-mantle service
        # call context, limiting exposure to the intended inference path.
        gateway_role.add_to_policy(iam.PolicyStatement(
            sid="BedrockMantleBearerToken",
            actions=["bedrock-mantle:CallWithBearerToken"],
            resources=["*"],
            conditions={"StringEquals": {"aws:CalledViaLast": "bedrock-mantle.amazonaws.com"}},
        ))

        # ---- Policy engine + static Cedar policy ----
        self.policy_engine = agentcore.CfnPolicyEngine(
            self, "PolicyEngine",
            name=f"TokenomicsPolicyEngine_{suffix.replace('-', '_')}",
            description="Tokenomics budget-enforcement policy engine.",
        )
        # Gateway role can evaluate the policy engine.
        # GetPolicyEngine is scoped to the policy engine ARN.
        gateway_role.add_to_policy(iam.PolicyStatement(
            sid="PolicyEngineConfiguration",
            actions=["bedrock-agentcore:GetPolicyEngine"],
            resources=[self.policy_engine.attr_policy_engine_arn],
        ))
        # AuthorizeAction / PartiallyAuthorizeActions must cover BOTH the policy engine AND
        # the gateway resource: at gateway-create time AgentCore runs a policy-engine check
        # (GenesisPolicyEngineCheck) that calls AuthorizeAction ON THE GATEWAY ARN. We use a
        # gateway/* wildcard scoped to this account+region because the concrete gateway ARN
        # (random suffix) is only known at create time -- referencing it here would create a
        # circular dependency (role -> gateway -> role). Matches the AWS-documented gateway
        # execution-role policy.
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html
        gateway_role.add_to_policy(iam.PolicyStatement(
            sid="PolicyEngineAuthorization",
            actions=[
                "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions",
            ],
            resources=[
                self.policy_engine.attr_policy_engine_arn,
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
            ],
        ))

        # ---- Gateway (CUSTOM_JWT via Cognito; interceptor attached; policy engine wired) ----
        self.gateway = agentcore.CfnGateway(
            self, "Gateway",
            name=f"tokenomics-gw-{suffix}",
            role_arn=gateway_role.role_arn,
            protocol_type="MCP",
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=oidc_discovery_url,
                    allowed_clients=[self.user_pool_client.user_pool_client_id],
                    # The gateway scope check accepts the scope carried by the in-app Cognito
                    # access token (USER_SRP / USER_PASSWORD auth), which is
                    # `aws.cognito.signin.user.admin`. Developers (and their IDEs) authenticate
                    # with their own credentials in-app -- NOT via the Hosted UI redirect -- so
                    # they never receive the custom `tokenomics-gateway/invoke` scope (Cognito
                    # only issues custom resource-server scopes through the OAuth code /
                    # client-credentials flows). The scope check here is only the OUTER gate
                    # ("a valid token from THIS pool + app client"); the REAL per-developer
                    # authorization is enforced downstream by the interceptor Lambda (maps the
                    # JWT `sub` -> project/budget/tier, fail-closed for unmapped identities) and
                    # the Cedar policy engine (blocks over-budget requests). See
                    # docs/SECURITY_ACCEPTED_RISKS.md.
                    # Production alternative: use the Hosted UI authorization-code flow to mint a
                    # `tokenomics-gateway/invoke`-scoped token and set that here instead.
                    allowed_scopes=["aws.cognito.signin.user.admin"],
                )
            ),
            interceptor_configurations=[
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=self.interceptor_fn.function_arn,
                        )
                    ),
                    interception_points=["REQUEST"],
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=True,
                    ),
                ),
                # RESPONSE interceptor: meters prompt-cache tokens from the buffered response.
                # We DO need the response body here (to read `usage`), so no RESPONSE_BODY payload
                # filter is applied. NOTE (AWS doc): Lambda sync invoke has a 6 MB request+response
                # payload cap; a base64 body near that size can exceed it. Inference `usage` lives
                # in the normal (non-huge) completion body, and the meter parses-then-passes-through
                # (returns an empty http object), so it adds no payload of its own. If very large
                # completions ever trip the cap, add a payload filter that keeps a minimal body.
                # HTTP-target response interceptors run in buffered (non-streaming) mode only.
                # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=self.cache_meter_fn.function_arn,
                        )
                    ),
                    interception_points=["RESPONSE"],
                ),
            ],
            policy_engine_configuration=agentcore.CfnGateway.GatewayPolicyEngineConfigurationProperty(
                arn=self.policy_engine.attr_policy_engine_arn,
                mode="ENFORCE",
            ),
        )
        # CRITICAL ordering: CfnGateway is an L1 construct that references the role only by
        # `role_arn` (a string), so CDK does NOT auto-infer a dependency on the role's inline
        # policy. Without this, CloudFormation creates the Gateway in parallel with the role's
        # DefaultPolicy -- and AgentCore runs a SYNCHRONOUS policy-engine check (GetPolicyEngine
        # / AuthorizeAction using the gateway role) at create time. If the policy isn't attached
        # yet, that check fails with AccessDenied and the whole stack rolls back. Force the
        # gateway to wait for the role AND its attached permissions to exist first.
        self.gateway.node.add_dependency(gateway_role)
        if gateway_role.node.try_find_child("DefaultPolicy") is not None:
            self.gateway.node.add_dependency(gateway_role.node.find_child("DefaultPolicy"))

        # ---- mantle inference target ----
        self.gateway_target = agentcore.CfnGatewayTarget(
            self, "MantleTarget",
            gateway_identifier=self.gateway.ref,
            name=target_name,
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                inference=agentcore.CfnGatewayTarget.InferenceTargetConfigurationProperty(
                    connector=agentcore.CfnGatewayTarget.InferenceConnectorTargetConfigurationProperty(
                        source=agentcore.CfnGatewayTarget.InferenceConnectorSourceProperty(
                            connector_id="bedrock-mantle",
                        )
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE",
                )
            ],
        )

        # ---- Static Cedar policy: forbid when the interceptor flags over-budget ----
        # Per-user state lives in DynamoDB (read by the interceptor, which injects
        # context.input.budget_exceeded). This ONE policy never changes per developer.
        gateway_arn = self.gateway.attr_gateway_arn
        cedar_statement = Fn.sub(
            "forbid(principal, action, resource == AgentCore::Gateway::\"${GwArn}\") "
            "when { context has input && context.input has budget_exceeded "
            "&& context.input.budget_exceeded == true };",
            {"GwArn": gateway_arn},
        )
        self.budget_policy = agentcore.CfnPolicy(
            self, "ForbidBudgetExceeded",
            name="ForbidBudgetExceeded",
            policy_engine_id=self.policy_engine.attr_policy_engine_id,
            enforcement_mode="ACTIVE",
            validation_mode="IGNORE_ALL_FINDINGS",
            description="Hard-stop: deny inference when the developer is over their daily budget.",
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(statement=cedar_statement)
            ),
        )

        # ---- Static Cedar policy: PERMIT normal requests to this gateway ----
        # Cedar is DENY-BY-DEFAULT: with only a `forbid` policy, every request is denied because
        # nothing explicitly permits it ("No policy applies to the request"). This permit allows
        # requests to our gateway; the `forbid ... when budget_exceeded` above still overrides it
        # (in Cedar, an explicit forbid always beats a permit), so the net behavior is:
        # allow normally, block when the developer is over budget. Real per-developer
        # authentication/mapping is still enforced upstream by the interceptor (fail-closed).
        permit_statement = Fn.sub(
            "permit(principal, action, resource == AgentCore::Gateway::\"${GwArn}\");",
            {"GwArn": gateway_arn},
        )
        self.permit_policy = agentcore.CfnPolicy(
            self, "PermitGatewayRequests",
            name="PermitGatewayRequests",
            policy_engine_id=self.policy_engine.attr_policy_engine_id,
            enforcement_mode="ACTIVE",
            validation_mode="IGNORE_ALL_FINDINGS",
            description="Permit requests to the gateway (overridden by ForbidBudgetExceeded).",
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(statement=permit_statement)
            ),
        )

        # ==================================================================
        # Phase 4: metering analytics -- Metric Stream -> Firehose -> enrich
        #          Lambda -> S3 (near-real-time per-model / per-cost-center)
        # ==================================================================

        # ---- Analytics S3 bucket (enriched metric records; read directly by the API) ----
        self.analytics_bucket = s3.Bucket(
            self, "AnalyticsBucket",
            bucket_name=f"tokenomics-analytics-{self.account}-{suffix}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---- Enrich transform Lambda (Firehose processor) ----
        enrich_role = iam.Role(
            self, "EnrichRole",
            role_name=f"Tokenomics-Enrich-{suffix}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        enrich_role.add_to_policy(iam.PolicyStatement(
            sid="ScopedLambdaLogs",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[
                f"arn:aws:logs:{self.region}:{self.account}:"
                f"log-group:/aws/lambda/Tokenomics-Enrich-{suffix}:*"
            ],
        ))
        # Read-only on the config table (query the by-project GSI for tags).
        self.config_table.grant_read_data(enrich_role)

        self.enrich_fn = lambda_.Function(
            self, "EnrichFn",
            function_name=f"Tokenomics-Enrich-{suffix}",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/gw_enrich"),
            role=enrich_role,
            timeout=Duration.seconds(60),
            memory_size=256,
            log_group=self._lambda_log_group(
                "EnrichLogGroup", f"Tokenomics-Enrich-{suffix}"),
            environment={
                "CONFIG_TABLE": self.config_table.table_name,
                "PROJECT_INDEX": "by-project",
            },
        )

        # ---- Firehose delivery role (write S3 + invoke transform) ----
        firehose_role = iam.Role(
            self, "FirehoseRole",
            role_name=f"Tokenomics-Firehose-{suffix}",
            assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
        )
        self.analytics_bucket.grant_read_write(firehose_role)
        self.enrich_fn.grant_invoke(firehose_role)

        # ---- Firehose delivery stream (Direct PUT from Metric Stream) ----
        self.firehose_stream = firehose.CfnDeliveryStream(
            self, "MetricsFirehose",
            delivery_stream_name=f"Tokenomics-Metrics-{suffix}",
            delivery_stream_type="DirectPut",
            extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=self.analytics_bucket.bucket_arn,
                role_arn=firehose_role.role_arn,
                prefix="mantle/",
                error_output_prefix="errors/",
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=60, size_in_m_bs=1
                ),
                compression_format="GZIP",
                processing_configuration=firehose.CfnDeliveryStream.ProcessingConfigurationProperty(
                    enabled=True,
                    processors=[firehose.CfnDeliveryStream.ProcessorProperty(
                        type="Lambda",
                        parameters=[
                            firehose.CfnDeliveryStream.ProcessorParameterProperty(
                                parameter_name="LambdaArn",
                                parameter_value=self.enrich_fn.function_arn,
                            ),
                            firehose.CfnDeliveryStream.ProcessorParameterProperty(
                                parameter_name="BufferIntervalInSeconds", parameter_value="60"
                            ),
                            firehose.CfnDeliveryStream.ProcessorParameterProperty(
                                parameter_name="BufferSizeInMBs", parameter_value="1"
                            ),
                        ],
                    )],
                ),
            ),
        )

        # ---- CloudWatch Metric Stream role (write to Firehose) ----
        metric_stream_role = iam.Role(
            self, "MetricStreamRole",
            role_name=f"Tokenomics-MetricStream-{suffix}",
            assumed_by=iam.ServicePrincipal("streams.metrics.cloudwatch.amazonaws.com"),
        )
        metric_stream_role.add_to_policy(iam.PolicyStatement(
            actions=["firehose:PutRecord", "firehose:PutRecordBatch"],
            resources=[self.firehose_stream.attr_arn],
        ))

        # ---- Metric Stream: only the 4 token metrics from AWS/BedrockMantle ----
        # TotalInput/OutputTokens (project-level) + InputTokens/OutputTokens (per-model, for
        # per-model cost). Non-cost metrics (Inferences, errors, burndown, reservation) dropped.
        self.metric_stream = cloudwatch.CfnMetricStream(
            self, "MantleMetricStream",
            name=f"Tokenomics-Mantle-{suffix}",
            firehose_arn=self.firehose_stream.attr_arn,
            role_arn=metric_stream_role.role_arn,
            output_format="json",
            include_filters=[
                cloudwatch.CfnMetricStream.MetricStreamFilterProperty(
                    namespace="AWS/BedrockMantle",
                    metric_names=[
                        "TotalInputTokens", "TotalOutputTokens",
                        "InputTokens", "OutputTokens",
                    ],
                ),
                # Prompt-cache tokens (custom namespace, dims Project+Model) so the analytics
                # data lake can report historical cache usage + savings alongside input/output.
                cloudwatch.CfnMetricStream.MetricStreamFilterProperty(
                    namespace="Tokenomics/Cache",
                    metric_names=[
                        "CacheReadInputTokens", "CacheWriteInputTokens",
                    ],
                ),
            ],
        )

        # ==================================================================
        # Phase 5: governance aggregator (5-min) + pricing refresh (daily)
        # ==================================================================
        global_default_budget = str(config.get("global_default_budget_usd", 5))

        # ---- Pricing refresh Lambda (daily + on-demand from admin console) ----
        pricing_role = iam.Role(
            self, "PricingRefreshRole",
            role_name=f"Tokenomics-PricingRefresh-{suffix}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        pricing_role.add_to_policy(iam.PolicyStatement(
            sid="ScopedLambdaLogs",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[
                f"arn:aws:logs:{self.region}:{self.account}:"
                f"log-group:/aws/lambda/Tokenomics-PricingRefresh-{suffix}:*"
            ],
        ))
        self.pricing_table.grant_read_write_data(pricing_role)
        # The routable model catalog now comes from the bedrock-mantle `GET /v1/models` endpoint
        # (the authoritative list of what the gateway can serve, incl. GPT-5 / Claude that the
        # price list omits), not bedrock:ListFoundationModels. ListModels has no resource-level
        # ARN, so it is granted on "*". (Left un-region-conditioned: the mantle ".api.aws"
        # endpoint does not reliably populate aws:RequestedRegion -- gating on it denied the
        # call, same gotcha as the mantle project actions.)
        pricing_role.add_to_policy(iam.PolicyStatement(
            sid="ListMantleModels",
            actions=["bedrock-mantle:ListModels"],
            resources=["*"],
        ))
        # AWS Price List API. NOTE: the `pricing` service defines NO service-specific condition
        # keys (e.g. there is no `pricing:ServiceCode`) and no resource-level ARNs -- it only
        # supports Resource "*". A prior attempt to constrain this with a `pricing:ServiceCode`
        # condition caused every GetProducts call to be DENIED at runtime (the condition can
        # never match), which left all models with price_available=false. Grant read-only on "*"
        # with no invalid condition. The ServiceCode is passed as a REQUEST PARAMETER in the API
        # call itself (ServiceCode="AmazonBedrock"), not an IAM condition.
        pricing_role.add_to_policy(iam.PolicyStatement(
            sid="PricingApiReadOnly",
            actions=["pricing:GetProducts", "pricing:DescribeServices"],
            resources=["*"],
        ))
        self.pricing_refresh_fn = lambda_.Function(
            self, "PricingRefreshFn",
            function_name=f"Tokenomics-PricingRefresh-{suffix}",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/gw_pricing_refresh"),
            role=pricing_role,
            timeout=Duration.minutes(5),
            memory_size=256,
            log_group=self._lambda_log_group(
                "PricingRefreshLogGroup", f"Tokenomics-PricingRefresh-{suffix}"),
            environment={"PRICING_TABLE": self.pricing_table.table_name},
        )
        events.Rule(
            self, "PricingRefreshSchedule",
            rule_name=f"Tokenomics-PricingRefresh-{suffix}",
            schedule=events.Schedule.cron(hour="1", minute="0"),  # daily 01:00 UTC
            targets=[targets.LambdaFunction(self.pricing_refresh_fn)],
        )

        # ---- Governance aggregator Lambda (every 5 minutes) ----
        aggregator_role = iam.Role(
            self, "AggregatorRole",
            role_name=f"Tokenomics-Aggregator-{suffix}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        aggregator_role.add_to_policy(iam.PolicyStatement(
            sid="ScopedLambdaLogs",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[
                f"arn:aws:logs:{self.region}:{self.account}:"
                f"log-group:/aws/lambda/Tokenomics-Aggregator-{suffix}:*"
            ],
        ))
        self.config_table.grant_read_data(aggregator_role)
        self.pricing_table.grant_read_data(aggregator_role)
        self.budget_table.grant_write_data(aggregator_role)
        # CloudWatch read actions (GetMetricData / ListMetrics) support neither resource-level
        # ARNs nor the cloudwatch:namespace condition key -- attaching that condition makes the
        # statement never match (implicit deny). They must be granted on "*" with no condition.
        # (GetMetricData reads are already scoped in-handler to the AWS/BedrockMantle namespace.)
        aggregator_role.add_to_policy(iam.PolicyStatement(
            sid="CloudWatchMetricsRead",
            actions=["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"],
            resources=["*"],
        ))
        self.aggregator_fn = lambda_.Function(
            self, "AggregatorFn",
            function_name=f"Tokenomics-Aggregator-{suffix}",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/gw_aggregator"),
            role=aggregator_role,
            timeout=Duration.minutes(2),
            memory_size=256,
            log_group=self._lambda_log_group(
                "AggregatorLogGroup", f"Tokenomics-Aggregator-{suffix}"),
            environment={
                "CONFIG_TABLE": self.config_table.table_name,
                "BUDGET_TABLE": self.budget_table.table_name,
                "PRICING_TABLE": self.pricing_table.table_name,
                "PROJECT_INDEX": "by-project",
                "GLOBAL_DEFAULT_BUDGET_USD": global_default_budget,
                "CACHE_NAMESPACE": "Tokenomics/Cache",
            },
        )
        events.Rule(
            self, "AggregatorSchedule",
            rule_name=f"Tokenomics-Aggregator-{suffix}",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[targets.LambdaFunction(self.aggregator_fn)],
        )

        # ==================================================================
        # Phase 8: backend API (HTTP API + Cognito JWT authorizer + Lambda)
        # ==================================================================
        api_role = iam.Role(
            self, "ApiRole",
            role_name=f"Tokenomics-Api-{suffix}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        api_role.add_to_policy(iam.PolicyStatement(
            sid="ScopedLambdaLogs",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[
                f"arn:aws:logs:{self.region}:{self.account}:"
                f"log-group:/aws/lambda/Tokenomics-Api-{suffix}:*"
            ],
        ))
        self.config_table.grant_read_write_data(api_role)
        self.budget_table.grant_read_data(api_role)
        self.pricing_table.grant_read_data(api_role)
        # List Cognito users for developer assignment (scoped to this user pool).
        api_role.add_to_policy(iam.PolicyStatement(
            sid="CognitoListUsers",
            actions=["cognito-idp:ListUsers"],
            resources=[self.user_pool.user_pool_arn],
        ))
        # Invoke the pricing-refresh Lambda (manual refresh, Option A).
        self.pricing_refresh_fn.grant_invoke(api_role)
        # On-demand budget recompute: admin "Refresh budgets now" (POST /admin/refresh-budgets)
        # and developer self-service (POST /me/refresh) both invoke the aggregator Lambda, which
        # is a global idempotent sweep. The 5-min EventBridge schedule still runs independently.
        self.aggregator_fn.grant_invoke(api_role)
        # Create/list mantle projects. CreateProject and ListProjects do NOT authorize against
        # a specific project ARN: at create time the project doesn't exist yet, and list is a
        # collection-level operation. AWS's own docs scope CreateProject with Resource "*"
        # (see the "Deny project creation" example). Granting these on project/* yields a 403.
        # https://docs.aws.amazon.com/bedrock/latest/userguide/security-iam-projects.html
        # NOTE: no aws:RequestedRegion condition here. bedrock-mantle uses the newer ".api.aws"
        # endpoint, and gating CreateProject/ListProjects on aws:RequestedRegion causes the
        # request to be DENIED (403) at runtime -- the condition key is not populated as expected
        # for this endpoint, so it never matches. Verified: with the condition the create 403s;
        # without it the create succeeds. These actions have no resource-level ARN at create time
        # (the project doesn't exist yet), so Resource is "*".
        api_role.add_to_policy(iam.PolicyStatement(
            sid="MantleProjectCreateList",
            actions=[
                "bedrock-mantle:CreateProject",
                "bedrock-mantle:ListProjects",
            ],
            resources=["*"],
        ))
        # GetProject targets an existing project, and TagResource is REQUIRED by CreateProject
        # whenever the request body includes tags (the handler always tags projects with
        # Group/CostCenter/Owner/Type). CloudTrail confirmed the create-project 403 was
        # specifically a missing bedrock-mantle:TagResource permission. Both are scoped to
        # project ARNs in this account/region (tagging targets the just-created project).
        # GetProject + tag management on the developer's mantle project. On reassignment the
        # API updates the project's tags (add_tags); mantle's tag-update internally reads the
        # current tags first, so BOTH TagResource AND ListTagsForResource are required.
        # CloudTrail confirmed the exact denied actions on project/<id>: first TagResource
        # (CreateProject-with-tags), then ListTagsForResource (add_tags on reassignment).
        api_role.add_to_policy(iam.PolicyStatement(
            sid="MantleProjectReadTag",
            actions=[
                "bedrock-mantle:GetProject",
                "bedrock-mantle:TagResource",
                "bedrock-mantle:ListTagsForResource",
            ],
            resources=[f"arn:aws:bedrock-mantle:{self.region}:{self.account}:project/*"],
        ))
        # ---- Analytics (GET /admin/analytics) ----
        # The API reads the enriched metric records directly from S3 (list mantle/ prefixes,
        # GetObject + gunzip + parse). No Athena/Glue/Lake Formation -- the data volume is small
        # and a direct scan is fast and dependency-free.
        self.analytics_bucket.grant_read(api_role)

        self.api_fn = lambda_.Function(
            self, "ApiFn",
            function_name=f"Tokenomics-Api-{suffix}",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/gw_api"),
            role=api_role,
            timeout=Duration.seconds(60),  # room for the analytics S3 scan + aggregation
            memory_size=256,
            log_group=self._lambda_log_group("ApiLogGroup", f"Tokenomics-Api-{suffix}"),
            environment={
                "CONFIG_TABLE": self.config_table.table_name,
                "BUDGET_TABLE": self.budget_table.table_name,
                "PRICING_TABLE": self.pricing_table.table_name,
                "USER_POOL_ID": self.user_pool.user_pool_id,
                "PRICING_REFRESH_FN": self.pricing_refresh_fn.function_name,
                "AGGREGATOR_FN": self.aggregator_fn.function_name,
                "PROJECT_INDEX": "by-project",
                "ADMIN_GROUP": "admins",
                "ANALYTICS_BUCKET": self.analytics_bucket.bucket_name,
                # ID-token-only enforcement on the control-plane API. Observe-mode rollout is
                # complete: verified both frontends send the Cognito ID token to this API (an
                # ID-token /admin/* call returns 200) and no legitimate non-ID-token traffic
                # appeared in the observe logs. Now in "enforce" -- access tokens (the IDE's
                # gateway/inference credential) are rejected here with 401, so a leaked inference
                # credential cannot be replayed against /admin/* or /me/*.
                "TOKEN_USE_MODE": "enforce",
            },
        )

        # HTTP API with a Cognito JWT authorizer (validates tokens from THIS user pool).
        jwt_authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "TokenomicsJwtAuthorizer",
            jwt_issuer=(
                f"https://cognito-idp.{self.region}.amazonaws.com/"
                f"{self.user_pool.user_pool_id}"
            ),
            identity_source=["$request.header.Authorization"],
            jwt_audience=[self.user_pool_client.user_pool_client_id],
        )
        self.http_api = apigwv2.HttpApi(
            self, "HttpApi",
            api_name=f"Tokenomics-Api-{suffix}",
            # CORS is set below (after the CloudFront distributions are created) so the allowed
            # origins can reference the portal/admin distribution domains. Those domains are
            # generated by CloudFront at deploy time; setting CORS here would require hardcoding
            # them (a two-step deploy). Using CloudFormation token references instead lets a
            # single deploy resolve the real domains -- see the cors_configuration block below.
        )
        api_integration = apigwv2_integrations.HttpLambdaIntegration(
            "ApiIntegration", handler=self.api_fn
        )
        # All routes go through the same Lambda; the Lambda enforces group-based authz.
        # IMPORTANT: bind the JWT authorizer to the REAL methods only (GET/POST/DELETE/PUT) --
        # NOT ANY. If OPTIONS is included, the authorizer runs on the browser's CORS preflight
        # (which carries no token) and returns 401, so the browser reports "failed to fetch".
        # By omitting OPTIONS from the authorized routes, HTTP API answers the preflight itself
        # (via cors_preflight below) with an unauthenticated 204 + CORS headers.
        app_methods = [
            apigwv2.HttpMethod.GET,
            apigwv2.HttpMethod.POST,
            apigwv2.HttpMethod.PUT,
            apigwv2.HttpMethod.DELETE,
        ]
        self.http_api.add_routes(
            path="/admin/{proxy+}",
            methods=app_methods,
            integration=api_integration,
            authorizer=jwt_authorizer,
        )
        self.http_api.add_routes(
            path="/me/{proxy+}",
            methods=app_methods,
            integration=api_integration,
            authorizer=jwt_authorizer,
        )

        # API access logging: capture every request (method, path, status, requestId, source ip,
        # latency) to CloudWatch so there is an audit trail of API activity and authorization
        # outcomes at the edge, complementing the structured AUDIT records the Lambda emits.
        api_access_logs = logs.LogGroup(
            self, "ApiAccessLogs",
            log_group_name=f"/tokenomics/api-access-{suffix}",
            retention=logs.RetentionDays.ONE_YEAR,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # HttpApi auto-creates a $default stage; reach its CfnStage to set access log settings
        # (the L2 HttpStage doesn't expose access logging directly).
        _default_stage = self.http_api.default_stage.node.default_child
        _default_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=api_access_logs.log_group_arn,
            format=json.dumps({
                "requestId": "$context.requestId",
                "ip": "$context.identity.sourceIp",
                "requestTime": "$context.requestTime",
                "httpMethod": "$context.httpMethod",
                "routeKey": "$context.routeKey",
                "path": "$context.path",
                "status": "$context.status",
                "protocol": "$context.protocol",
                "responseLength": "$context.responseLength",
                "integrationErrorMessage": "$context.integrationErrorMessage",
                "authorizerError": "$context.authorizer.error",
            }),
        )

        # ------------------------------------------------------------------
        # Admin console hosting (S3 + CloudFront)
        # ------------------------------------------------------------------
        # A private bucket fronted by CloudFront with Origin Access Control (OAC) -- the
        # current AWS-recommended way for CloudFront to read a private S3 bucket (replaces the
        # deprecated Origin Access Identity). The frontend bucket is disposable (DESTROY +
        # auto_delete_objects) since it only holds built static assets.
        import os as _os

        admin_bucket = s3.Bucket(
            self, "AdminBucket",
            bucket_name=f"tokenomics-admin-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )
        # CloudFront is a GLOBAL service, so OriginAccessControl names must be unique across the
        # whole account -- not just per region. The name CDK auto-derives from the
        # (region-agnostic) stack name is identical when the same stack is deployed to a second
        # region, causing an AlreadyExists collision. Give each OAC a region-scoped name so the
        # same stack deploys cleanly to any region (matches the tokenomics-*-{region} bucket naming).
        admin_oac = cloudfront.S3OriginAccessControl(
            self, "AdminOAC",
            origin_access_control_name=f"tokenomics-admin-oac-{self.region}",
        )
        admin_distribution = cloudfront.Distribution(
            self, "AdminDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    admin_bucket, origin_access_control=admin_oac),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
            # SPA fallback: client-side routing means deep links 404 at S3; serve index.html.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200, response_page_path="/index.html"
                ),
                cloudfront.ErrorResponse(
                    http_status=404, response_http_status=200, response_page_path="/index.html"
                ),
            ],
            comment=f"Tokenomics admin console ({self.region})",
        )
        # ------------------------------------------------------------------
        # Runtime frontend config (env.js) — written into each hosting bucket from THIS stack's
        # own outputs, so the built bundle carries NO account/region/URL values and one build is
        # portable to any account + region. The SPA loads /env.js (classic script) before boot,
        # setting window.__ENV__, which src/config.js reads. Values below are CloudFormation
        # tokens; Fn.sub resolves them at deploy time, and Source.data materializes the file via
        # a bucket-deployment custom resource.
        gateway_inference_url = Fn.sub(
            "https://${GwId}.gateway.bedrock-agentcore.${Region}.amazonaws.com/inference",
            {"GwId": self.gateway.ref, "Region": self.region},
        )

        def _env_js(include_gateway: bool) -> str:
            # window.__ENV__ assignment. Admin needs API/pool/region; portal also needs the
            # gateway URL + virtual model. virtual_model is a plain Python string (safe to embed).
            fields = {
                "API_URL": self.http_api.api_endpoint,
                "USER_POOL_ID": self.user_pool.user_pool_id,
                "USER_POOL_CLIENT_ID": self.user_pool_client.user_pool_client_id,
                "REGION": self.region,
            }
            if include_gateway:
                fields["GATEWAY_URL"] = gateway_inference_url
                fields["VIRTUAL_MODEL"] = virtual_model
            # Build a JSON object literal using Fn.sub placeholders for the token-valued fields.
            variables = {}
            parts = []
            for i, (k, v) in enumerate(fields.items()):
                token = f"V{i}"
                variables[token] = v
                parts.append(f'"{k}":"${{{token}}}"')
            body = "{" + ",".join(parts) + "}"
            return Fn.sub("window.__ENV__ = " + body + ";", variables)

        # Deploy the built assets only when they exist, so the stack still synthesizes before
        # the frontend has been built (the two-step: build dist -> deploy).
        _admin_dist = "frontend-admin/dist"
        if _os.path.isdir(_admin_dist):
            s3_deploy.BucketDeployment(
                self, "AdminDeployment",
                sources=[
                    s3_deploy.Source.asset(_admin_dist),
                    s3_deploy.Source.data("env.js", _env_js(include_gateway=False)),
                ],
                destination_bucket=admin_bucket,
                distribution=admin_distribution,
                distribution_paths=["/*"],
            )

        # ------------------------------------------------------------------
        # Developer portal hosting (S3 + CloudFront) -- 2nd distribution
        # ------------------------------------------------------------------
        # Same pattern as the admin console: private bucket + CloudFront (OAC) + SPA fallback.
        # The portal reuses the same Cognito app client (self.user_pool_client) -- that client is
        # already the gateway's allowed_client, so developer access tokens minted here clear the
        # gateway's authorizer. Developers sign in with their own credentials; every API call is
        # scoped to their own /me/* data and the chat routes through the gateway.
        portal_bucket = s3.Bucket(
            self, "PortalBucket",
            bucket_name=f"tokenomics-portal-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )
        portal_oac = cloudfront.S3OriginAccessControl(
            self, "PortalOAC",
            origin_access_control_name=f"tokenomics-portal-oac-{self.region}",
        )
        portal_distribution = cloudfront.Distribution(
            self, "PortalDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    portal_bucket, origin_access_control=portal_oac),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200, response_page_path="/index.html"
                ),
                cloudfront.ErrorResponse(
                    http_status=404, response_http_status=200, response_page_path="/index.html"
                ),
            ],
            comment=f"Tokenomics developer portal ({self.region})",
        )
        _portal_dist = "frontend-portal/dist"
        if _os.path.isdir(_portal_dist):
            s3_deploy.BucketDeployment(
                self, "PortalDeployment",
                sources=[
                    s3_deploy.Source.asset(_portal_dist),
                    s3_deploy.Source.data("env.js", _env_js(include_gateway=True)),
                ],
                destination_bucket=portal_bucket,
                distribution=portal_distribution,
                distribution_paths=["/*"],
            )

        # ------------------------------------------------------------------
        # ------------------------------------------------------------------
        # CORS (scoped to the two frontend origins) -- set on the API's L1 now that the
        # CloudFront distributions exist. allow_origins references their domain names as
        # CloudFormation tokens, so CloudFormation creates the distributions first and injects the
        # real https://<dist>.cloudfront.net origins at deploy time (no hardcoded domain, no
        # two-step deploy). Preflight behavior is unchanged: the JWT authorizer is still bound to
        # GET/POST/PUT/DELETE only (not OPTIONS), so HTTP API answers the browser's preflight
        # itself with a 204 + these CORS headers, while real requests carry the ID token.
        # Methods/headers are narrowed to exactly what the frontends use (was ANY + "*").
        _cfn_http_api = self.http_api.node.default_child  # aws_apigatewayv2.CfnApi
        _cfn_http_api.cors_configuration = apigwv2.CfnApi.CorsProperty(
            allow_origins=[
                f"https://{admin_distribution.distribution_domain_name}",
                f"https://{portal_distribution.distribution_domain_name}",
            ],
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["authorization", "content-type"],
            max_age=600,
        )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "AdminUrl", value=f"https://{admin_distribution.distribution_domain_name}")
        CfnOutput(self, "AdminBucketName", value=admin_bucket.bucket_name)
        CfnOutput(self, "PortalUrl", value=f"https://{portal_distribution.distribution_domain_name}")
        CfnOutput(self, "PortalBucketName", value=portal_bucket.bucket_name)
        CfnOutput(self, "ConfigTableName", value=self.config_table.table_name)
        CfnOutput(self, "BudgetTableName", value=self.budget_table.table_name)
        CfnOutput(self, "PricingTableName", value=self.pricing_table.table_name)
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(
            self, "HostedUiBaseUrl",
            value=f"https://{self.user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com",
        )
        CfnOutput(
            self, "OidcDiscoveryUrl",
            value=(
                f"https://cognito-idp.{self.region}.amazonaws.com/"
                f"{self.user_pool.user_pool_id}/.well-known/openid-configuration"
            ),
        )
        CfnOutput(self, "GatewayId", value=self.gateway.ref)
        CfnOutput(self, "GatewayArn", value=self.gateway.attr_gateway_arn)
        CfnOutput(
            self, "GatewayInferenceUrl",
            value=Fn.sub(
                "https://${GwId}.gateway.bedrock-agentcore.${Region}.amazonaws.com/inference",
                {"GwId": self.gateway.ref, "Region": self.region},
            ),
        )
        CfnOutput(self, "PolicyEngineId", value=self.policy_engine.attr_policy_engine_id)
        CfnOutput(self, "InterceptorFunctionName", value=self.interceptor_fn.function_name)
        CfnOutput(self, "AnalyticsBucketName", value=self.analytics_bucket.bucket_name)
        CfnOutput(self, "MetricStreamName", value=self.metric_stream.name)

        CfnOutput(self, "ApiEndpoint", value=self.http_api.api_endpoint)
