# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Seed the Tokenomics demo with realistic customer projects, developers, and budget states.

This makes the whole demo reproducible after a fresh `cdk deploy`: the CDK stack creates the
infrastructure (empty), and this script populates the runtime data that lives in DynamoDB +
Cognito so the admin console and developer portal light up with the full governance story
(per-developer cost tracking, cross-family tier ladders, downgrade + block).

What it does (idempotent -- safe to re-run):
  1. Cognito: create the demo developer users (+ the two role users) with a known password,
     confirmed, in the `developers` / `admins` groups.
  2. Customer projects: create 3 policy-only projects (Platform Engineering / Data Science /
     Mobile Apps) with cross-family tier ladders (premium -> mid -> open-source) via the
     backend API handler (so mantle projects are provisioned correctly on assignment).
  3. Assign each developer to a project -- this creates each developer's OWN bedrock-mantle
     project (per-developer cost identity).
  4. Seed each developer's Budget row to a preset budget % so the console immediately shows
     every enforcement state: healthy (tier1), warning (tier2), critical (tier3), blocked.

NOTE on budget seeding: the aggregator Lambda recomputes budgets every ~5 min from live
CloudWatch metrics and will overwrite these seeded values (with $0 when there is no real
traffic). For a demo, run this script shortly before presenting; the seeded values persist
until the next aggregator run. Use --no-budget to skip budget seeding and rely on real usage.

Usage:
    python scripts/seed_demo.py --region <region>
    python scripts/seed_demo.py --user-pool-id <region>_xxx --api-fn Tokenomics-Api-<region> \
        --config-table Tokenomics-Config-<region> --budget-table Tokenomics-Budget-<region>

If the resource names/ids are not supplied, they are resolved from the CloudFormation stack
outputs (stack name defaults to TokenomicsStack).
"""
import argparse
import json
import os
import sys
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError


def _load_env_local(keys):
    """Read KEY=VALUE lines from a .env.local file (project root or scripts/) into os.environ,
    without overwriting values already set in the real environment. Only the requested keys are
    loaded. This lets an operator supply secrets (e.g. the demo password) in an uncommitted
    .env.local file instead of hardcoding them in source. .env.local should be gitignored."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, ".env.local"),
        os.path.join(os.path.dirname(here), ".env.local"),  # project root
    ]
    wanted = set(keys)
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k in wanted and not os.environ.get(k):
                        os.environ[k] = v
        except OSError:
            continue


def _resolve_password(env_var, role_label):
    """Resolve a demo password from an env var (or an uncommitted .env.local), with NO hardcoded
    fallback so source disclosure never reveals a working credential. Precedence: process env,
    then .env.local. Exits with guidance if unset.

    Min length is 12 to match the Cognito pool password policy (a shorter value would be set here
    but then rejected at first real sign-in)."""
    _load_env_local([env_var])
    pw = os.environ.get(env_var)
    if not pw:
        sys.exit(
            f"{env_var} is not set (needed for the {role_label} password). Provide it:\n"
            f"  export {env_var}='<a-strong-password>'\n"
            "or add a line to scripts/.env.local (or the project-root .env.local):\n"
            f"  {env_var}=<a-strong-password>\n"
            "There is intentionally NO default password (avoids a publicly-known credential)."
        )
    if len(pw) < 12:
        sys.exit(f"{env_var} is too short; use a strong password (>= 12 chars, matching the pool policy).")
    return pw


# Two distinct demo passwords, resolved at runtime from the environment or an uncommitted
# .env.local (NO hardcoded default). Splitting them removes the escalation where a developer
# who is handed the demo developer password thereby also knows the admins-group password:
#   * DEVELOPER_PASSWORD -> the six developer accounts (portal logins)
#   * ADMIN_PASSWORD     -> the admins-group account(s) only (admin console)
# ADMIN_PASSWORD is REQUIRED and must be different; the developer password does NOT fall back to
# it. (The developer password may fall back to nothing else -- it is its own required value.)
DEVELOPER_PASSWORD = _resolve_password("TOKENOMICS_DEMO_PASSWORD", "developer")
ADMIN_PASSWORD = _resolve_password("TOKENOMICS_ADMIN_PASSWORD", "admin")
if ADMIN_PASSWORD == DEVELOPER_PASSWORD:
    sys.exit(
        "TOKENOMICS_ADMIN_PASSWORD must be DIFFERENT from TOKENOMICS_DEMO_PASSWORD.\n"
        "Sharing one value lets any developer sign in as the admins-group account and take over "
        "the control plane. Set a distinct admin password."
    )

# Project TEMPLATES -- metadata only, NO hardcoded model ids. The concrete tier1/2/3 models are
# chosen at runtime from the region's LIVE model catalog (the Pricing table, populated by the
# pricing-refresh Lambda from mantle GET /v1/models), classified by which gateway ROUTE each
# family serves on. This keeps the seed portable to any account/region and route-correct:
#   * route="openai"    -> tiers picked from OpenAI-compatible families (served on
#                          /v1/chat/completions): the dev-portal chat uses this route.
#   * route="anthropic" -> tiers are anthropic.claude-* (served on /v1/messages): use this
#                          project for the Anthropic path (Claude Code / dual-protocol portal),
#                          and it's the one that produces prompt-cache tokens.
PROJECT_TEMPLATES = [
    {"name": "Platform Engineering", "group": "platform", "cost_center": "CC-1001",
     "daily_budget_usd": 25, "thresholds": [75, 90, 100], "route": "openai"},
    {"name": "Data Science", "group": "data-science", "cost_center": "CC-2002",
     "daily_budget_usd": 40, "thresholds": [75, 90, 100], "route": "openai"},
    {"name": "AI Research", "group": "ai-research", "cost_center": "CC-3003",
     "daily_budget_usd": 30, "thresholds": [75, 90, 100], "route": "anthropic"},
]

# Families that serve on the gateway's OpenAI-compatible routes — either Chat Completions
# (/v1/chat/completions) for open-weight models, or the Responses API (/v1/responses) for
# proprietary GPT models. The developer portal auto-detects which route to use from the model
# prefix, so the seed doesn't need to distinguish between them — only that they are NOT
# Anthropic. Verified live: openai.gpt-oss-* -> Chat Completions 200; openai.gpt-5.* ->
# Responses API 200; deepseek/qwen/mistral/gemma -> Chat Completions 200.
# xai.grok is intentionally excluded (not accepted on any portal-supported route).
_OPENAI_ROUTE_PREFIXES = ("openai.", "deepseek.", "qwen.", "mistral.",
                          "google.gemma", "google.gemini", "meta.", "nvidia.", "zai.")

# Families excluded from seeded tier ladders so a fresh deploy works with NO extra opt-in:
#   * xai.*  -> not served on Chat Completions or Responses API (portal-unsupported).
#   * anthropic.claude-fable*  -> require the `aws_review` data-retention mode (their
#     allowed_modes = ["aws_review","provider_data_share"], excluding "none"/"default"), so a
#     default-retention account gets HTTP 400 "data retention mode 'default' is not available
#     for this model". Excluding them keeps the demo zero-config; opt in per the data-retention
#     docs if you specifically want Fable. See:
#     https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html
_EXCLUDED_PREFIXES = ("xai.", "anthropic.claude-fable")


def _classify_route(model_id: str) -> str | None:
    """Return the gateway route family for a model id:
      'anthropic' -> /v1/messages (anthropic.claude-*)
      'openai'    -> /v1/chat/completions or /v1/responses (the portal auto-detects which).
                     Includes openai.gpt-oss-*, openai.gpt-5.*, deepseek.*, qwen.*, etc.
      None        -> not usable by the portal (xai.grok, unknown families).
    """
    # Exclusions first (e.g. anthropic.claude-fable is Anthropic-family but retention-gated).
    if any(model_id.startswith(p) for p in _EXCLUDED_PREFIXES):
        return None
    if model_id.startswith("anthropic."):
        return "anthropic"
    if any(model_id.startswith(p) for p in _OPENAI_ROUTE_PREFIXES):
        return "openai"
    return None


def _pick_tiers(candidates: list[str]) -> list[str]:
    """Pick 3 tier models from an ordered candidate list. Pads by repeating the last available
    model if fewer than 3 exist, so a sparse region still yields a valid ladder."""
    picked = candidates[:3]
    if not picked:
        return []
    while len(picked) < 3:
        picked.append(picked[-1])
    return picked


def build_projects(ddb, pricing_table_name):
    """Build concrete project policies from the templates + the region's live model catalog.

    Reads model ids from the Pricing table, classifies each by route, and fills tier1/2/3 for
    each template from the appropriate route's models. Returns the list of project dicts ready
    for POST/PUT /admin/projects. Raises if the catalog is empty (pricing-refresh hasn't run).
    """
    table = ddb.Table(pricing_table_name)
    items = []
    resp = table.scan(ProjectionExpression="model_id")
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ProjectionExpression="model_id",
                          ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    all_ids = sorted({it["model_id"] for it in items if it.get("model_id")})
    if not all_ids:
        sys.exit(
            f"Pricing table {pricing_table_name} is EMPTY -- run the pricing-refresh first "
            "(admin console 'Refresh models', or invoke Tokenomics-PricingRefresh-<region>) "
            "so the region's model catalog is populated, then re-run this seed."
        )

    by_route = {"anthropic": [], "openai": []}
    for mid in all_ids:
        r = _classify_route(mid)
        if r:
            by_route[r].append(mid)
    # Deterministic preference: openai.* first, then the other OpenAI-route families; anthropic
    # sorted so the "biggest/opus" style ids tend to land on tier1 (sort is a stable proxy).
    by_route["openai"].sort(key=lambda m: (not m.startswith("openai."), m))
    by_route["anthropic"].sort()

    projects = []
    for tpl in PROJECT_TEMPLATES:
        route = tpl["route"]
        pool = by_route.get(route, [])
        if not pool:
            print(f"[project] WARNING: no {route}-route models in catalog for '{tpl['name']}'; "
                  "falling back to OpenAI-route models")
            pool = by_route.get("openai", [])
        tiers = _pick_tiers(pool)
        if not tiers:
            sys.exit(f"No routable models available to build project '{tpl['name']}'")
        p = {k: tpl[k] for k in ("name", "group", "cost_center", "daily_budget_usd", "thresholds")}
        p["tier1_model"], p["tier2_model"], p["tier3_model"] = tiers[0], tiers[1], tiers[2]
        # allowed_models = the 3 tiers plus a little headroom from the same route pool so the
        # dev can also explicitly request any of them without being downgraded.
        p["allowed_models"] = list(dict.fromkeys(tiers + pool[:5]))
        projects.append(p)
        print(f"[project] {tpl['name']} [{route}] tiers -> {tiers}")
    return projects

# Developers: (username, email, project name, seeded budget %). The % chosen to hit every
# enforcement state so the console shows tier1/tier2/tier3/blocked at a glance.
DEVELOPERS = [
    ("bob",   "bob@tokenomics.demo",   "Platform Engineering", 40.0),   # healthy  -> tier1
    ("dev",   "dev@tokenomics.demo",   "Platform Engineering", 78.0),   # warning  -> tier2 (portal chat, OpenAI route)
    ("carol", "carol@tokenomics.demo", "Data Science",         100.0),  # blocked  -> 403
    ("dave",  "dave@tokenomics.demo",  "Data Science",         80.0),   # warning  -> tier2
    # alice + erin on the Anthropic-route project: their portal chat (dual-protocol) + Claude
    # Code both hit anthropic.claude-* and can generate prompt-cache tokens. Kept at healthy
    # tier1 so they are not downgraded/blocked during the cache demo.
    ("alice", "alice@tokenomics.demo", "AI Research",          40.0),   # healthy  -> tier1 (Anthropic)
    ("erin",  "erin@tokenomics.demo",  "AI Research",          30.0),   # healthy  -> tier1 (Anthropic)
]

# The two role users (created if missing).
ROLE_USERS = [("admin", "admin@tokenomics.demo", "admins")]


def _stack_outputs(cf, stack_name):
    try:
        resp = cf.describe_stacks(StackName=stack_name)
    except ClientError as e:
        sys.exit(f"Could not read stack {stack_name}: {e}")
    outs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
    return outs


def ensure_user(cognito, pool_id, username, email, group):
    """Create a Cognito user (idempotent), set a permanent password, add to a group.

    The password is role-scoped: admins-group users get ADMIN_PASSWORD, everyone else gets
    DEVELOPER_PASSWORD. This ensures a developer handed the demo developer password does not
    thereby also know the admin credential."""
    try:
        cognito.admin_create_user(
            UserPoolId=pool_id, Username=username,
            UserAttributes=[{"Name": "email", "Value": email},
                            {"Name": "email_verified", "Value": "true"}],
            MessageAction="SUPPRESS",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "UsernameExistsException":
            raise
    password = ADMIN_PASSWORD if group == "admins" else DEVELOPER_PASSWORD
    cognito.admin_set_user_password(
        UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
    try:
        cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    # Return the user's sub (stable id used as the Config key).
    u = cognito.admin_get_user(UserPoolId=pool_id, Username=username)
    return {a["Name"]: a["Value"] for a in u["UserAttributes"]}.get("sub")


def _invoke_admin(lam, api_fn, method, path, body):
    """Invoke the backend API Lambda with a synthetic admin event (bypasses HTTP/JWT for
    seeding; the handler still runs its group-authz on the injected claims)."""
    event = {
        "requestContext": {
            "http": {"method": method, "path": path},
            # Simulate an authenticated admin. token_use="id" is required because the API
            # enforces ID-token-only on the control plane (TOKEN_USE_MODE=enforce); the seed
            # stands in for an admin who would present a Cognito ID token, so the injected
            # claims must include it or the handler returns 401.
            "authorizer": {"jwt": {"claims": {
                "sub": "seed-admin", "cognito:groups": "admins", "token_use": "id"}}},
        },
        "body": json.dumps(body) if body is not None else None,
    }
    resp = lam.invoke(FunctionName=api_fn, InvocationType="RequestResponse",
                      Payload=json.dumps(event).encode("utf-8"))
    payload = json.loads(resp["Payload"].read())
    status = payload.get("statusCode")
    parsed = json.loads(payload["body"]) if payload.get("body") else {}
    return status, parsed


def main():
    ap = argparse.ArgumentParser(description="Seed the Tokenomics demo data.")
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--stack-name", default="TokenomicsStack")
    ap.add_argument("--user-pool-id")
    ap.add_argument("--api-fn")
    ap.add_argument("--config-table")
    ap.add_argument("--budget-table")
    ap.add_argument("--pricing-table")
    ap.add_argument("--no-budget", action="store_true",
                    help="skip seeding synthetic budget %% (rely on real usage instead)")
    args = ap.parse_args()

    session = boto3.session.Session(region_name=args.region)
    cf = session.client("cloudformation")
    cognito = session.client("cognito-idp")
    lam = session.client("lambda")
    ddb = session.resource("dynamodb")

    outs = _stack_outputs(cf, args.stack_name)
    pool_id = args.user_pool_id or outs.get("UserPoolId")
    api_fn = args.api_fn or outs.get("InterceptorFunctionName", "").replace("Interceptor", "Api") \
        or f"Tokenomics-Api-{args.region}"
    if not api_fn or "Api" not in api_fn:
        api_fn = f"Tokenomics-Api-{args.region}"
    config_table = args.config_table or outs.get("ConfigTableName") or f"Tokenomics-Config-{args.region}"
    budget_table = args.budget_table or outs.get("BudgetTableName") or f"Tokenomics-Budget-{args.region}"
    pricing_table = args.pricing_table or outs.get("PricingTableName") or f"Tokenomics-Pricing-{args.region}"

    if not pool_id:
        sys.exit("Could not resolve UserPoolId (pass --user-pool-id or ensure stack outputs exist)")

    print(f"Region={args.region} pool={pool_id} api_fn={api_fn}")
    print(f"config_table={config_table} budget_table={budget_table} pricing_table={pricing_table}\n")

    # Build concrete, route-correct project policies from the region's LIVE model catalog.
    projects = build_projects(ddb, pricing_table)
    print()

    # 1. Role users.
    for username, email, group in ROLE_USERS:
        sub = ensure_user(cognito, pool_id, username, email, group)
        print(f"[user] {username} ({email}) group={group} sub={sub}")

    # 2. Customer projects (idempotent): reuse an existing project with the same name (updating
    # its policy), else create a new policy-only cproj_* row. Avoids duplicating projects on
    # re-runs.
    _, existing = _invoke_admin(lam, api_fn, "GET", "/admin/projects", None)
    by_name = {p.get("name"): p.get("project_id") for p in existing.get("projects", [])}
    name_to_cproj = {}
    for p in projects:
        if p["name"] in by_name:
            cproj = by_name[p["name"]]
            status, resp = _invoke_admin(lam, api_fn, "PUT", f"/admin/projects/{cproj}", p)
            name_to_cproj[p["name"]] = cproj
            print(f"[project] updated {p['name']} -> {cproj} ({status})")
        else:
            status, resp = _invoke_admin(lam, api_fn, "POST", "/admin/projects", p)
            if status == 201:
                name_to_cproj[p["name"]] = resp["project_id"]
                print(f"[project] created {p['name']} -> {resp['project_id']}")
            else:
                print(f"[project] {p['name']} create returned {status}: {resp}")

    # 3. Developers: create user, assign to project (provisions per-dev mantle project).
    dev_subs = {}
    for username, email, project_name, _pct in DEVELOPERS:
        group = "admins" if username == "admin" else "developers"
        sub = ensure_user(cognito, pool_id, username, email, group)
        dev_subs[username] = sub
        cproj = name_to_cproj.get(project_name)
        if not cproj:
            print(f"[assign] SKIP {username}: project {project_name} not created")
            continue
        status, resp = _invoke_admin(lam, api_fn, "POST", "/admin/assign",
                                     {"sub": sub, "project_id": cproj, "email": email})
        if status == 200:
            print(f"[assign] {username} -> {project_name} (mantle {resp.get('mantle_project_id')})")
        else:
            print(f"[assign] {username} -> {project_name} returned {status}: {resp}")
        time.sleep(0.3)  # gentle pacing for mantle project creation

    # 4. Seed each developer's Budget row to the preset % (unless --no-budget).
    if args.no_budget:
        print("\n[budget] skipped (--no-budget)")
        return

    config = ddb.Table(config_table)
    budget = ddb.Table(budget_table)
    print()
    for username, email, project_name, pct in DEVELOPERS:
        sub = dev_subs.get(username)
        if not sub:
            continue
        cfg = config.get_item(Key={"sub": sub}).get("Item")
        if not cfg:
            print(f"[budget] SKIP {username}: no config row")
            continue
        mantle_pid = cfg.get("mantle_project_id") or cfg.get("project_id")
        daily = float(cfg.get("daily_budget_usd", 0) or 0) or 10.0
        cost = round(daily * pct / 100.0, 4)
        in_tok = int(cost * 60000)   # rough synthetic token split for display
        out_tok = int(cost * 12000)
        budget.put_item(Item={
            "project_id": mantle_pid,
            "date": time.strftime("%Y-%m-%d"),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_today_usd": Decimal(str(cost)),
            "daily_budget_usd": Decimal(str(daily)),
            "budget_pct": Decimal(str(pct)),
            "budget_exceeded": pct >= 100.0,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        print(f"[budget] {username}: {pct}% (${cost} of ${daily}) on {mantle_pid}")

    print("\nDone. Open the admin console (Usage / Enforcement / Alerts) to see the seeded state.")
    print("NOTE: the aggregator recomputes budgets every ~5 min and will overwrite these with")
    print("real (currently $0) usage. Re-run this script shortly before demoing.")


if __name__ == "__main__":
    main()
