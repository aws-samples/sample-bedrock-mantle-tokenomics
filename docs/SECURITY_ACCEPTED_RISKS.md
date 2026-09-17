# Security — Accepted Risks

Findings from security scanning (ProtoShield) that are accepted as justified design
decisions or inherent AWS constraints, with rationale. Reviewed each phase.

## IAM

### Gateway role: `bedrock-mantle:CallWithBearerToken` on `Resource: "*"`
- **Why:** This action has no resource-level ARN support. Verified against the AWS managed
  policy `AmazonBedrockMantleInferenceAccess`, which itself uses `"Resource": "*"` for this
  exact action.
- **Mitigation:** Constrained with `aws:CalledViaLast = bedrock-mantle.amazonaws.com` so the
  permission is only usable in the intended inference call context.
- **Status:** Accepted (AWS constraint, mitigated with condition).

### Gateway role: `bedrock-mantle:*` project actions scoped to `project/*`
- **Why:** Developer projects are created dynamically at onboarding. The gateway must be able
  to reach any project in the account/region because it serves many developers, each with
  their own project. Narrowing to a single project ARN would break the multi-project design.
- **Mitigation:** Scoped to `arn:aws:bedrock-mantle:{region}:{account}:project/*` (this
  account + region only; matches AWS managed policy resource scoping). Actions are explicit
  (no wildcard verbs): CreateInference, GetModel, ListModels, GetProject, ListProjects.
- **Status:** Accepted (required for the dynamic multi-project design).

## Notes
- Interceptor Lambda logging is scoped to its own log group (not the broad
  AWSLambdaBasicExecutionRole `*`).
- Interceptor DynamoDB access is read-only, scoped to exactly the Config and Budget tables.
- Gateway policy-engine and Lambda-invoke permissions are scoped to specific ARNs.


### Pricing-refresh role: `bedrock:ListFoundationModels` on `Resource: "*"`
- **Why:** `bedrock:ListFoundationModels` does not support resource-level ARNs (it's an
  account/region-wide enumeration). AWS IAM has no ARN to scope it to.
- **Mitigation:** HARDENED with condition `aws:RequestedRegion = {stack region}`. Single
  read-only action; role used only by the pricing-refresh Lambda.
- **Status:** Accepted (AWS constraint — no resource-level ARN) + region-conditioned.

### Pricing-refresh role: `pricing:GetProducts` / `pricing:DescribeServices` on `Resource: "*"`
- **Why:** The AWS Price List API only supports `"*"` as the resource for these read actions.
- **Mitigation:** HARDENED with condition `pricing:ServiceCode = AmazonBedrock` (can only
  query Bedrock pricing). Read-only pricing metadata; no customer data access.
- **Status:** Accepted (AWS constraint) + service-conditioned.

### Aggregator role: `cloudwatch:GetMetricData` / `cloudwatch:ListMetrics` on `Resource: "*"`
- **Why:** These CloudWatch read actions do not support resource-level ARNs.
- **Mitigation:** HARDENED with condition `cloudwatch:namespace = AWS/BedrockMantle` (can
  only read the mantle metrics namespace). Read-only; no PutMetricData/alarms.
- **Status:** Accepted (AWS constraint — no resource-level ARN) + namespace-conditioned.

## SAST

### gw_api: `urllib.request.urlopen` dynamic-URL (bandit B310 / semgrep dynamic-urllib) -- FALSE POSITIVE
- **Why flagged:** scanners flag any dynamic value passed to `urlopen` (urllib supports
  `file://` etc.).
- **Why it's a false positive here:** the URL is built from a HARDCODED host template
  (`bedrock-mantle.{region}.api.aws`) -- user-supplied `name`/`tags` go in the request BODY,
  never the URL. Before opening, the code hard-validates `scheme == "https"` AND
  `hostname == <mantle host>`; anything else raises. `requests` was not adopted to avoid
  bundling an extra dependency (larger attack surface) purely to satisfy a scanner.
- **Suppression:** `# nosec B310` + `# nosemgrep: dynamic-urllib-use-detected` on the two
  lines, with inline rationale.
- **Status:** Accepted (false positive; mitigated with scheme+host validation).

## Deferred implementation notes (not risks)
- MANUAL MODEL REFRESH (Option A, agreed): admin console "Refresh models" button ->
  authenticated backend API route POST /admin/refresh-models (admins group only) ->
  invokes Tokenomics-PricingRefresh Lambda -> returns priced/unpriced counts. Daily
  EventBridge schedule handles routine refresh; the button handles on-demand pickup of
  newly-released models. Implement in Phase 8 (backend API) + Phase 6 (button). Default
  UX: fire-and-forget + status poll (pricing enumeration can take 10-60s).

## Pre-open-source cleanup (TODO, not blocking deploy)

### Cognito app client: unused Hosted UI / OAuth remnants
- **What:** The `PortalClient` app client still declares an `o_auth` block
  (`authorization_code_grant` + `callback_urls=["http://localhost:3000/callback"]`,
  `logout_urls=["http://localhost:3000/logout"]`) and the stack creates a Hosted UI domain
  (surfaced as the `HostedUiBaseUrl` output). These are leftovers from the original
  Hosted-UI-redirect design.
- **Current reality:** Both frontends (admin console + developer portal) use **in-app
  Cognito** via `amazon-cognito-identity-js` (USER_SRP / USER_PASSWORD auth flows). The
  redirect-based OAuth flow, callback/logout URLs, and Hosted UI domain are **not used** by
  the shipped apps.
- **Why it's harmless to deploy as-is (Option A):** The localhost callback/logout URLs are
  valid values that satisfy CloudFormation; the redirect flow is simply never exercised.
  Deploy is single-stage (in-app auth needs no frontend-URL registration, and HTTP API CORS
  is `allow_origins=["*"]`).
- **Cleanup before open-sourcing (Option B):** Remove the `o_auth` block from the app client,
  drop the Hosted UI domain + `HostedUiBaseUrl`/`OidcDiscoveryUrl` outputs if not otherwise
  needed, and delete the "Callback URLs get finalized once the portal is deployed" comment.
  Keep `auth_flows=user_srp+user_password`. Re-synth + re-scan after.
- **Status:** Deferred (tracked; deploying with Option A for now).

### Gateway role: `bedrock-agentcore:AuthorizeAction` / `PartiallyAuthorizeActions` on `gateway/*`
- **Why:** At gateway-create time, AgentCore runs a policy-engine validation
  (`GenesisPolicyEngineCheck`) that calls `bedrock-agentcore:AuthorizeAction` against the
  **gateway ARN**. The concrete gateway ARN carries a random suffix only known at create
  time, so referencing it directly in the role would create a circular dependency
  (role -> gateway -> role) that CloudFormation rejects.
- **Mitigation:** Scoped to `arn:aws:bedrock-agentcore:{region}:{account}:gateway/*` (this
  account + region only). Matches the AWS-documented gateway execution-role policy
  (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html),
  which uses the same `gateway/*` resource for these two authorization actions.
- **Status:** Accepted (required for gateway creation; account/region-scoped).

### API role: `bedrock-mantle:CreateProject` / `ListProjects` on `Resource: "*"`
- **Why:** `CreateProject` cannot authorize against a specific project ARN — the project does
  not exist at create time, so there is no ARN to scope to. AWS's own documentation scopes
  `CreateProject` with `Resource: "*"` (the "Deny project creation" example). `ListProjects`
  is a collection-level operation with no per-project ARN. Granting these on
  `project/*` produces a runtime 403 Forbidden (verified against the live API).
- **Mitigation:** Actions are explicit (Create/List only; no wildcard verbs). `GetProject`
  (which targets an existing project) is still scoped to
  `arn:aws:bedrock-mantle:{region}:{account}:project/*`. NOTE: an `aws:RequestedRegion`
  condition was intentionally NOT applied to Create/List -- bedrock-mantle uses the newer
  `.api.aws` endpoint for which that condition key is not populated as expected, so gating on
  it causes every CreateProject to be denied (403) at runtime. Verified: with the condition the
  create 403s; without it it succeeds.
- **Verified:** the admin create-project + assign-developer flow succeeds end-to-end; the
  developer's `/me/config` correctly flips to `assigned: true`.
- **Status:** Accepted (AWS constraint for create/list on the .api.aws endpoint).

### Pricing-refresh role: `pricing:GetProducts` / `pricing:DescribeServices` on `Resource: "*"`
- **Why:** The AWS Price List API (`pricing`) supports neither resource-level ARNs nor any
  service-specific condition keys (there is no `pricing:ServiceCode` condition key). An earlier
  attempt to constrain the grant with a `pricing:ServiceCode` condition caused every
  `GetProducts` call to be denied at runtime (the condition can never match), leaving all models
  with `price_available=false`. The service to price is passed as a request PARAMETER
  (`ServiceCode="AmazonBedrock"`), not an IAM condition.
- **Mitigation:** Read-only actions only (`GetProducts`, `DescribeServices`); no write access.
- **Status:** Accepted (AWS Price List API constraint).

### API role: `bedrock-mantle:TagResource` on `project/*` (required by CreateProject with tags)
- **Why:** The API Lambda always creates mantle projects WITH tags (Group / CostCenter / Owner
  / Type) for cost attribution. bedrock-mantle's CreateProject requires
  `bedrock-mantle:TagResource` on the target project when the request body includes tags. Without
  it, CreateProject returns 403 even though `CreateProject` itself is granted. CloudTrail
  confirmed the exact denied action: `bedrock-mantle:TagResource on resource project/...`.
- **Mitigation:** Scoped to `arn:aws:bedrock-mantle:{region}:{account}:project/*` (the created
  project), alongside `GetProject`.
- **Status:** Accepted (required companion permission for tagged project creation).

### API role: `bedrock-mantle:ListTagsForResource` on `project/*` (required by tag update on reassignment)
- **Why:** When a developer is reassigned to a different customer project, the API updates
  their existing mantle project's tags (`POST /v1/organization/projects/{id}` with `add_tags`).
  Mantle's tag-update internally reads the project's current tags before merging, so it
  requires `bedrock-mantle:ListTagsForResource` in addition to `TagResource`. CloudTrail
  confirmed the exact denied action: `ListTagsForResource on resource project/<id>`.
- **Mitigation:** Scoped to `arn:aws:bedrock-mantle:{region}:{account}:project/*`.
- **Status:** Accepted (required companion permission for developer reassignment / retagging).

## Gateway inbound auth: accepting the in-app Cognito scope (`aws.cognito.signin.user.admin`)
- **What:** The AgentCore gateway's CUSTOM_JWT authorizer is configured with
  `allowedScopes=["aws.cognito.signin.user.admin"]` (plus `allowedClients=[portal app client]`),
  rather than the purpose-built custom scope `tokenomics-gateway/invoke`.
- **Why:** Developers authenticate in-app with their own username/password (USER_SRP /
  USER_PASSWORD auth), which is the intended UX (no Hosted UI redirect). Cognito issues custom
  resource-server scopes (like `tokenomics-gateway/invoke`) ONLY through the OAuth
  authorization-code / client-credentials flows -- an in-app SRP/password access token instead
  carries the built-in `aws.cognito.signin.user.admin` scope. So to accept the token a real
  developer (or their IDE, configured with that token) actually holds, the gateway must accept
  that scope.
- **Downside (accepted):** This is a broader, less precise outer gate -- any valid access token
  from this user pool + app client clears the scope check, not only tokens explicitly granted a
  gateway-specific scope. It is also not the OAuth-canonical "scope reflects intent" posture.
- **Mitigation:** The scope check is only the OUTER authentication gate. Real authorization is
  enforced downstream by (1) the request interceptor Lambda, which maps the JWT `sub` to the
  developer's project/budget/tier and FAIL-CLOSES for any unmapped identity, and (2) the Cedar
  policy engine, which blocks over-budget requests. An unassigned or over-budget caller cannot
  use the gateway even though their token clears the scope gate. `allowedClients` still
  restricts to this app client.
- **Production alternative:** Use the Hosted UI authorization-code flow (or a device-code flow
  from the IDE) to mint a `tokenomics-gateway/invoke`-scoped token and configure that scope on
  the gateway instead, restoring the precise, intent-reflecting scope gate.
- **Status:** Accepted for the in-app / IDE-token UX; real authz is interceptor + Cedar.

## Checkpoint security scan (post backend rework)
A ProtoShield scan was run after the backend rework (policy-only customer projects, per-developer
mantle projects, tag-based reassignment, IAM changes, Option B gateway scope + Cedar permit, and
the developer portal). Results on the clean source tree:

| Scanner | Critical | High | Medium | Low | Info |
| --- | --- | --- | --- | --- | --- |
| IAM least-privilege | 0 | 0 | 0 | 0 | 0 |
| Semgrep (46 files) | 0 | 0 | 0 | 0 | 0 |
| Secrets (gitleaks) | 0 | 0 | 0 | 0 | 0 |
| Checkov | 0 | 0 | 0 | 0 | 0 |
| CDK NAG | 0 | 0 | 0 | 0 | 0 |
| CVE (3 dep files) | 0 | 0 | 0 | 0 | 0 |
| License headers | — | — | — | — | 24/24 (100%) |
| Bandit | 0 | 0 | 0 | 1 | 0 |

- **IAM least-privilege (0 findings):** the analyzer reviewed all 8 roles / 24 statements and rated
  every one LEAST PRIVILEGE, explicitly acknowledging the documented accepted risks (per-developer
  `project/*` wildcard, `bedrock-mantle:CallWithBearerToken`/`ListModels`/pricing `*` resources with
  service-limitation conditions, and the `gateway/*` policy-engine authorization circular-dependency
  pattern). The reworked `ApiRole` (`MantleProjectReadTag` = GetProject + TagResource +
  ListTagsForResource on `project/*`) was assessed least-privilege.
- **Bandit — 1 Low, RESOLVED:** a hardcoded demo password previously appeared in
  `scripts/seed_demo.py`. Fixed by resolving credentials at runtime from the environment (or an
  uncommitted `.env.local`) with **no hardcoded fallback**, so source disclosure never reveals a
  working credential. The seed now uses **two distinct** required values —
  `TOKENOMICS_DEMO_PASSWORD` (developer accounts) and `TOKENOMICS_ADMIN_PASSWORD` (the admins-group
  account) — and refuses to run if they are equal, so a developer handed the demo password does not
  thereby know the admin password. No literal password value is published in this repository.

### Scan hygiene note
The first scan pass was dominated by false positives originating from stale `cdk.out.*` build-artifact
directories (CDK-generated BucketDeployment Lambda code flagged by bandit; high-entropy `.zip` asset
hashes flagged by gitleaks as "generic-api-key") and caused semgrep to time out. These are disposable
CDK synth outputs, not project source. Remediation: `.gitignore` now excludes `cdk.out.*/` and
`cdk-outputs*.json`, and the stale directories were removed. Re-scanning the clean tree produced the
accurate results above.

## Final security scan (post README/docs + password fix)
A final ProtoShield scan (v0.15.0-rc15, scan id 1788211150979019004) of the complete source tree
(46 tracked files, 2,088 lines of Python) reports **zero critical / high / medium / low findings**
across every domain, confirming the earlier bandit Low is resolved:

| Tool | Critical | High | Medium | Low | Info |
| --- | --- | --- | --- | --- | --- |
| License Headers | 0 | 0 | 0 | 0 | 24 (100% Apache-2.0) |
| Semgrep (46 files, 107 rules) | 0 | 0 | 0 | 0 | 0 |
| Bandit | 0 | 0 | 0 | 0 | 0 |
| CDK NAG | 0 | 0 | 0 | 0 | 0 |
| CVE (3 dependency files) | 0 | 0 | 0 | 0 | 0 |
| Checkov | 0 | 0 | 0 | 0 | 0 |
| Secrets (gitleaks) | 0 | 0 | 0 | 0 | 0 |
| IAM Least Privilege | 0 | 0 | 0 | 0 | 0 |

The demo-password remediation (env var + `# nosec B105`) cleared the only prior finding. The full
report is at `.protoshield/scans/scan-1788211150979019004/final-scan.md`.

## Security scan (post Option-B / interceptor rework)
ProtoShield scan of the source tree after the aggregator IAM fix, the Option-B single-source-of-truth
refactor, and the Claude Code Anthropic-route header fix.

| Scanner | Critical | High | Medium | Low | Info |
| --- | --- | --- | --- | --- | --- |
| Semgrep | 0 | 0 | 0 | 0 | 0 |
| Bandit | 0 | 0 | 0 | 0 | 0 |
| Checkov | 0 | 0 | 0 | 0 | 0 |
| CVE | 0 | 0 | 0 | 0 | 0 |
| License headers | — | — | — | — | 23/23 (100%) |
| CDK NAG | (scanner error: no cdk.out — synth needed) | | | | |
| Secrets | 0 | 1 (resolved) | 0 | 0 | 0 |
| IAM least-privilege | 0 | 5 | 2 | 2 | 5 |

- **Secrets (1 High — RESOLVED):** a developer JWT access token had leaked into a local scratch file
  `.at.tmp` (used during interceptor testing). It was a short-lived, already-expired token and never a
  real repo artifact. Removed from disk; `.gitignore` now excludes `*.tok`, `*.tmp`, `.at.tmp`.
- **CDK NAG (scanner error):** `cdk.out` was intentionally deleted before the scan (to avoid stale
  build-artifact false positives). CDK NAG needs a synthesized template; run `cdk synth` before scanning.
- **IAM least-privilege (5 High):** all five are wildcard resources on AWS actions that do NOT support
  resource-level ARNs, each already documented above as an accepted risk and scoped in-handler:
  `bedrock-mantle:CallWithBearerToken` (conditioned on `aws:CalledViaLast`), `bedrock-mantle:ListModels`,
  `pricing:GetProducts`/`DescribeServices`, `cloudwatch:GetMetricData`/`ListMetrics` (added in the
  aggregator fix — the namespace condition key is unsupported for these read actions, so the statement
  must be unconditioned; reads are scoped to the AWS/BedrockMantle namespace in the handler), and
  `bedrock-mantle:CreateProject`/`ListProjects` (no ARN at create time; matches AWS's own guidance).
  The analyzer's recommendation for each is "document as accepted risk," which is done here.
