# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tokenomics backend API (behind API Gateway HTTP API + Cognito JWT authorizer).

Single Lambda that routes on method + path. The API Gateway JWT authorizer validates the
Cognito token; this handler additionally enforces group-based authorization:
  - /admin/*  requires the caller to be in the Cognito `admins` group.
  - /me/*     uses the caller's own `sub` (any authenticated developer).

Routes:
  GET    /admin/models              -> priced models from the Pricing table (dropdowns)
  GET    /admin/users               -> Cognito users (developer assignment)
  GET    /admin/projects            -> list customer projects (policy definitions)
  POST   /admin/projects            -> create customer project (policy-only Config row)
  DELETE /admin/projects/{id}       -> remove a customer project's config
  POST   /admin/assign              -> assign a developer (sub) to a project (per-dev mantle project)
  POST   /admin/refresh-models      -> invoke the pricing-refresh Lambda (manual refresh)
  POST   /admin/refresh-budgets     -> invoke the aggregator Lambda (recompute all budgets now)
  GET    /admin/usage               -> per-developer/project usage rollup
  GET    /admin/enforcement         -> live per-developer tier/block state
  GET    /admin/alerts              -> developers at/over 75% of budget
  GET    /admin/analytics           -> analytics (?days=7|30|90&dim=trend|cost_center|group|model|developer)
  GET    /me/budget                 -> caller's own budget state
  POST   /me/refresh                -> recompute budgets (aggregator) then return caller's budget
  GET    /me/config                 -> caller's own project/tier config

Project provisioning: creating a mantle Project is an HTTP call to the bedrock-mantle
Projects API (not boto3); we sign it with SigV4 using the Lambda's credentials.
"""
import json
import logging
import os
import urllib.request
import uuid
from urllib.parse import urlparse

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONFIG_TABLE = os.environ["CONFIG_TABLE"]
BUDGET_TABLE = os.environ["BUDGET_TABLE"]
PRICING_TABLE = os.environ["PRICING_TABLE"]
USER_POOL_ID = os.environ["USER_POOL_ID"]
PRICING_REFRESH_FN = os.environ["PRICING_REFRESH_FN"]
AGGREGATOR_FN = os.environ.get("AGGREGATOR_FN", "")
PROJECT_INDEX = os.environ.get("PROJECT_INDEX", "by-project")
REGION = os.environ.get("AWS_REGION", "us-east-2")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "admins")
# Control-plane API accepts only Cognito ID tokens. Access tokens (the credential the IDE uses
# for the gateway) must NOT be replayable against /admin/* or /me/*. Rollout is staged:
#   "observe" (default) -> log a non-ID token but still allow (confirm nothing legitimate breaks)
#   "enforce"           -> reject non-ID tokens with 401
# Both frontends already send the ID token to this API, so "enforce" is the intended end state.
TOKEN_USE_MODE = os.environ.get("TOKEN_USE_MODE", "observe").lower()

_ddb = boto3.resource("dynamodb")
_config = _ddb.Table(CONFIG_TABLE)
_budget = _ddb.Table(BUDGET_TABLE)
_pricing = _ddb.Table(PRICING_TABLE)
_cognito = boto3.client("cognito-idp")
_lambda = boto3.client("lambda")
_session = boto3.session.Session()


# --------------------------------------------------------------------------- helpers
def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _claims(event):
    # HTTP API JWT authorizer puts claims here.
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        return {}


def _reject_non_id_token(claims):
    """Return a 401 response if the caller presented anything other than a Cognito ID token, or
    None to proceed. This confines the IDE's access token to the gateway (inference) surface so a
    leaked inference credential cannot read/administer the control plane. Staged via
    TOKEN_USE_MODE: 'observe' logs but allows; 'enforce' rejects. An ID token has
    token_use == 'id'; a Cognito access token has token_use == 'access'."""
    token_use = (claims or {}).get("token_use")
    if token_use == "id":
        return None  # correct credential type
    # Not an ID token (access token, or missing claim).
    logger.warning("Non-ID token at control-plane API (token_use=%s, mode=%s)",
                   token_use, TOKEN_USE_MODE)
    if TOKEN_USE_MODE == "enforce":
        return _resp(401, {"error": "control-plane API requires an ID token"})
    return None  # observe mode: allow, just logged


def _groups(claims):
    g = claims.get("cognito:groups", "")
    if isinstance(g, list):
        return set(g)
    return set(str(g).replace("[", "").replace("]", "").replace(" ", "").split(",")) if g else set()


def _is_admin(claims):
    return ADMIN_GROUP in _groups(claims)


def _valid_sub_format(sub):
    """A Cognito `sub` is a v4 UUID. Reject anything that is not UUID-shaped BEFORE it is used as
    a Config table key or a mantle project owner, so a caller cannot inject an arbitrary/crafted
    identifier (e.g. a `__project__:`-prefixed string that would collide with a settings row)."""
    if not isinstance(sub, str):
        return False
    try:
        uuid.UUID(sub)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _sub_exists(sub):
    """Confirm the sub belongs to a real user in this pool. Cognito's `sub` attribute is filterable
    via ListUsers, so a single filtered lookup verifies existence without paginating the pool."""
    try:
        resp = _cognito.list_users(
            UserPoolId=USER_POOL_ID, Filter=f'sub = "{sub}"', Limit=1
        )
        return len(resp.get("Users", [])) > 0
    except ClientError:
        logger.warning("cognito sub existence lookup failed", exc_info=True)
        # Fail closed: if we cannot confirm the user exists, do not assign.
        return False


def _route(event):
    ctx = event.get("requestContext", {}).get("http", {})
    return ctx.get("method", "GET"), ctx.get("path", "/")


def _body(event):
    raw = event.get("body") or "{}"
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def _qs(event):
    return event.get("queryStringParameters") or {}


def _audit(action, sub, decision, **fields):
    """Emit a single structured audit record to CloudWatch Logs. Covers authorization decisions
    (allow/deny) and administrative mutations, so there is a durable trail of who did (or was
    denied) what. Prefixed with AUDIT so a metric filter / query can isolate these lines.
    We never log tokens or full request bodies -- only actor, action, decision, and key ids."""
    try:
        record = {"audit": True, "action": action, "sub": sub or "anonymous",
                  "decision": decision}
        record.update({k: v for k, v in fields.items() if v is not None})
        logger.info("AUDIT %s", json.dumps(record, default=str))
    except Exception:
        # Auditing must never break the request path.
        logger.warning("audit log emit failed for action=%s", action)


# --------------------------------------------------------------------------- handler
def lambda_handler(event, context):
    method, path = _route(event)
    claims = _claims(event)
    sub = claims.get("sub")

    # Credential-type gate: the control-plane API serves ID tokens only. Access tokens (the IDE's
    # gateway credential) are rejected here in enforce mode, so a leaked inference token can't be
    # replayed against /admin/* or /me/*. (Staged rollout via TOKEN_USE_MODE.)
    _tok_reject = _reject_non_id_token(claims)
    if _tok_reject is not None:
        _audit("token_use", sub, "deny", reason="non_id_token",
               token_use=claims.get("token_use"), path=path, method=method)
        return _tok_reject

    # /me/* : any authenticated developer, scoped to self.
    if path.startswith("/me/"):
        if not sub:
            _audit("me_access", sub, "deny", reason="unauthenticated", path=path, method=method)
            return _resp(401, {"error": "unauthenticated"})
        if path == "/me/budget":
            return _me_budget(sub)
        if path == "/me/config":
            return _me_config(sub)
        if path == "/me/refresh" and method == "POST":
            return _me_refresh(sub)
        return _resp(404, {"error": "not found"})

    # /admin/* : admins group only.
    if path.startswith("/admin/"):
        if not _is_admin(claims):
            # Audit every denied admin attempt (who tried to reach which admin route).
            _audit("admin_access", sub, "deny", reason="not_admin", path=path, method=method)
            return _resp(403, {"error": "admin group required"})
        if path == "/admin/models" and method == "GET":
            return _list_models()
        if path == "/admin/users" and method == "GET":
            return _list_users()
        if path == "/admin/projects" and method == "GET":
            return _list_projects()
        if path == "/admin/projects" and method == "POST":
            _audit("project_create", sub, "allow", name=(_body(event) or {}).get("name"))
            return _create_project(_body(event))
        if path.startswith("/admin/projects/") and method == "PUT":
            pid = path.rsplit("/", 1)[-1]
            _audit("project_update", sub, "allow", project_id=pid)
            return _update_project(pid, _body(event))
        if path.startswith("/admin/projects/") and method == "DELETE":
            pid = path.rsplit("/", 1)[-1]
            _audit("project_delete", sub, "allow", project_id=pid)
            return _delete_project(pid)
        if path == "/admin/assign" and method == "POST":
            _b = _body(event)
            _audit("developer_assign", sub, "allow",
                   target_sub=(_b or {}).get("sub"), project_id=(_b or {}).get("project_id"))
            return _assign_developer(_b)
        if path == "/admin/refresh-models" and method == "POST":
            return _refresh_models()
        if path == "/admin/refresh-budgets" and method == "POST":
            return _refresh_budgets()
        if path == "/admin/usage" and method == "GET":
            return _usage_rollup()
        if path == "/admin/enforcement" and method == "GET":
            return _enforcement_status()
        if path == "/admin/alerts" and method == "GET":
            return _alerts()
        if path == "/admin/analytics" and method == "GET":
            return _analytics(_qs(event))
        return _resp(404, {"error": "not found"})

    return _resp(404, {"error": "not found"})


# --------------------------------------------------------------------------- admin ops
def _list_models():
    """Return ALL routable models (the mantle catalog), each with a price_available flag.

    We intentionally do NOT filter to priced-only: the newest premium models (GPT-5, Claude)
    are routable but the AWS price list hasn't published their on-demand token price yet. They
    must still be selectable for a project's tier ladder (a common ladder is a premium model at
    tier1). Unpriced models are flagged so the console can show "price: n/a"; the aggregator
    fail-closes their cost to a conservative rate so budget math stays safe.
    """
    items = []
    scan = {}
    while True:
        resp = _pricing.scan(**scan)
        for it in resp.get("Items", []):
            items.append({
                "model_id": it["model_id"],
                "price_available": bool(it.get("price_available", False)),
                "input_price_per_1k": float(it.get("input_price_per_1k", 0) or 0),
                "output_price_per_1k": float(it.get("output_price_per_1k", 0) or 0),
            })
        if "LastEvaluatedKey" in resp:
            scan["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        else:
            break
    return _resp(200, {"models": sorted(items, key=lambda m: m["model_id"])})


def _list_users():
    users = []
    paginator = _cognito.get_paginator("list_users")
    for page in paginator.paginate(UserPoolId=USER_POOL_ID):
        for u in page.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
            users.append({
                "sub": attrs.get("sub"),
                "username": u.get("Username"),
                "email": attrs.get("email"),
                "status": u.get("UserStatus"),
            })
    return _resp(200, {"users": users})


def _list_projects():
    """List customer projects (the `__project__:<id>` policy rows) with their assigned
    developers. A developer row is any Config row whose `sub` is NOT a `__project__:` settings
    row; it is attached to its customer project via the `customer_project` attribute (falling
    back to `project_id` for older rows). The settings row itself is never counted as a
    developer.
    """
    project_rows = []   # the __project__: policy definitions
    dev_rows = []       # real developer assignment rows
    scan = {}
    while True:
        resp = _config.scan(**scan)
        for it in resp.get("Items", []):
            sub = it.get("sub", "")
            if sub.startswith("__project__:"):
                project_rows.append(it)
            elif it.get("project_id"):
                dev_rows.append(it)
        if "LastEvaluatedKey" in resp:
            scan["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        else:
            break

    projects = {}
    for it in project_rows:
        pid = it.get("project_id")
        if not pid:
            continue
        projects[pid] = {
            "project_id": pid,
            "name": it.get("name", pid),
            "group": it.get("group"),
            "cost_center": it.get("cost_center"),
            "daily_budget_usd": float(it.get("daily_budget_usd", 0) or 0),
            "allowed_models": it.get("allowed_models") or [],
            "tier1_model": it.get("tier1_model"),
            "tier2_model": it.get("tier2_model"),
            "tier3_model": it.get("tier3_model"),
            "thresholds": it.get("thresholds") or [75, 90, 100],
            "developers": [],
        }
    # Attach developers to their customer project.
    for it in dev_rows:
        cproj = it.get("customer_project") or it.get("project_id")
        proj = projects.get(cproj)
        if proj is not None:
            proj["developers"].append({
                "sub": it["sub"],
                "email": it.get("developer_email"),
                "mantle_project_id": it.get("mantle_project_id") or it.get("project_id"),
            })
    return _resp(200, {"projects": list(projects.values())})


def _create_project(body):
    """Create a CUSTOMER project: a policy definition only (approved models, tier ladder,
    thresholds, budget, group/cost-center). It does NOT create a bedrock-mantle project.

    Cost tracking is per-DEVELOPER: each developer gets their own mantle project when assigned
    (see _assign_developer), so the mantle "Project" dimension yields per-developer token
    metering. A customer project is just the shared policy that applies to its developers, so it
    only needs a local id (cproj_<uuid>), not a mantle project id.
    """
    name = body.get("name")
    if not name:
        return _resp(400, {"error": "name required"})

    project_id = f"cproj_{uuid.uuid4().hex[:16]}"
    _config.put_item(Item={
        "sub": f"__project__:{project_id}",
        "project_id": project_id,
        "name": name,
        "group": body.get("group", "default"),
        "cost_center": body.get("cost_center", "unassigned"),
        "daily_budget_usd": _pos_num(body.get("daily_budget_usd", 0)),
        "allowed_models": body.get("allowed_models", []),
        "tier1_model": body.get("tier1_model"),
        "tier2_model": body.get("tier2_model"),
        "tier3_model": body.get("tier3_model"),
        "thresholds": body.get("thresholds", [75, 90, 100]),
    })
    return _resp(201, {"project_id": project_id})


def _update_project(project_id, body):
    """Update a customer project's policy (allowed models, tier ladder, thresholds, budget,
    group/cost-center). Only the fields present in the body are changed. The project id and its
    assigned developers are unaffected here (developer policy is re-synced on next assignment or
    via the reassignment flow)."""
    key = {"sub": f"__project__:{project_id}"}
    existing = _config.get_item(Key=key).get("Item")
    if not existing:
        return _resp(404, {"error": "project not found"})

    updatable = {
        "name": lambda v: v,
        "group": lambda v: v,
        "cost_center": lambda v: v,
        "daily_budget_usd": _pos_num,
        "allowed_models": lambda v: v,
        "tier1_model": lambda v: v,
        "tier2_model": lambda v: v,
        "tier3_model": lambda v: v,
        "thresholds": lambda v: v,
    }
    for field, coerce in updatable.items():
        if field in body:
            existing[field] = coerce(body[field])
    _config.put_item(Item=existing)
    return _resp(200, {"updated": project_id})


def _delete_project(project_id):
    _config.delete_item(Key={"sub": f"__project__:{project_id}"})
    return _resp(200, {"deleted": project_id})


def _assign_developer(body):
    """Assign a developer to a CUSTOMER project.

    Per-developer cost tracking: each developer gets their OWN bedrock-mantle project, created
    once at first assignment. That personal mantle project is the developer's permanent cost
    identity -- the interceptor injects it as the `OpenAI-Project` header, and the Budget table
    is keyed on it, so mantle's Project dimension yields per-developer token metering.

    The developer's Config row therefore carries TWO ids:
      - project_id       = the developer's personal MANTLE project id  (per-dev; budget + OpenAI-Project)
      - customer_project = the cproj_* id of the customer project      (for policy source + rollups)

    On first assignment we create the mantle project (tagged with the customer project's
    Group/CostCenter/Project + the developer's email). On re-assignment to a different customer
    project we DO NOT create a new mantle project -- we update the existing project's tags and
    swap the policy (see _reassign path). This keeps the developer's cost history unbroken.
    """
    sub = body.get("sub")
    customer_project_id = body.get("project_id")
    email = body.get("email")
    if not sub or not customer_project_id:
        return _resp(400, {"error": "sub and project_id required"})
    # Validate the developer identifier before it becomes a Config key / mantle project owner.
    # Format first (cheap), then existence in the pool (fail-closed) -- prevents assigning an
    # arbitrary or crafted sub that no real user maps to.
    if not _valid_sub_format(sub):
        return _resp(400, {"error": "sub must be a valid user id"})
    if not _sub_exists(sub):
        return _resp(404, {"error": "user not found"})

    proj = _config.get_item(Key={"sub": f"__project__:{customer_project_id}"}).get("Item")
    if not proj:
        return _resp(404, {"error": "project not found"})

    tags = {
        "Type": "developer",
        "Developer": email or sub,
        "Project": proj.get("name", customer_project_id),
        "Group": proj.get("group", "default"),
        "CostCenter": proj.get("cost_center", "unassigned"),
    }

    existing = _config.get_item(Key={"sub": sub}).get("Item")
    mantle_project_id = existing.get("mantle_project_id") if existing else None

    if not mantle_project_id:
        # First assignment: create the developer's own mantle project.
        dev_label = (email or sub).split("@")[0]
        try:
            mantle_project_id = _create_mantle_project(f"dev-{dev_label}", tags)
        except Exception:
            logger.warning("Failed to create developer mantle project", exc_info=True)
            return _resp(502, {"error": "failed to create developer mantle project"})
    else:
        # Already onboarded -> reassignment: retag the existing mantle project (no new project).
        try:
            _update_mantle_project_tags(mantle_project_id, tags)
        except Exception:
            logger.warning("Failed to update developer mantle project tags", exc_info=True)
            return _resp(502, {"error": "failed to update developer mantle project"})

    # Write the developer's Config row. project_id = the dev's mantle project (per-dev budget +
    # OpenAI-Project). The dev row holds only IDENTITY fields; governance (budget / tier ladder /
    # thresholds / group / cost_center) is NOT copied here -- it is the SINGLE SOURCE OF TRUTH on
    # the customer-project policy row and is resolved via `customer_project` by the interceptor,
    # aggregator, and API. This prevents per-developer drift when a project's policy is edited.
    _config.put_item(Item={
        "sub": sub,
        "project_id": mantle_project_id,
        "mantle_project_id": mantle_project_id,
        "customer_project": customer_project_id,
        "developer_email": email,
    })
    return _resp(200, {"assigned": sub, "mantle_project_id": mantle_project_id,
                       "customer_project": customer_project_id})


def _refresh_models():
    _lambda.invoke(FunctionName=PRICING_REFRESH_FN, InvocationType="Event")
    return _resp(202, {"status": "refresh started"})


# On-demand budget recompute. The aggregator is a global, idempotent sweep (recomputes every
# project's Budget row from CloudWatch metrics), so both the admin button and a developer's
# self-service refresh invoke the same function.
def _invoke_aggregator(sync):
    if not AGGREGATOR_FN:
        raise RuntimeError("aggregator not configured")
    _lambda.invoke(
        FunctionName=AGGREGATOR_FN,
        InvocationType="RequestResponse" if sync else "Event",
        Payload=b"{}",
    )


def _refresh_budgets():
    # Admin: fire-and-forget; the sweep takes a few seconds and the console can re-poll.
    _invoke_aggregator(sync=False)
    return _resp(202, {"status": "budget refresh started"})


# Durable per-sub rate guard for developer self-service refresh. A DynamoDB conditional write in
# the Config table (key `__refresh__:<sub>`) enforces the cooldown ACROSS all Lambda containers
# and survives cold starts, so a developer (or many warm containers) cannot drive unbounded
# org-wide aggregator recomputes. The guard row carries a TTL so it self-cleans.
_REFRESH_COOLDOWN_S = 20


def _acquire_refresh_slot(sub) -> bool:
    """Atomically claim a refresh slot for `sub`. Returns True if the caller may invoke the
    aggregator now, False if a refresh happened within the cooldown window. Uses a conditional
    PutItem: the write succeeds only when there is no guard row or the existing one is older than
    the cooldown. Concurrent/repeat calls fail the condition and are throttled. Fails CLOSED on an
    unexpected error (returns False -> no recompute) so this can't become a DoS amplifier."""
    import time as _t
    now = int(_t.time())
    key = f"__refresh__:{sub}"
    try:
        _config.put_item(
            Item={
                "sub": key,
                "last_refresh": now,
                "ttl": now + 3600,  # self-clean after 1h (table must have TTL on `ttl` to expire)
            },
            ConditionExpression="attribute_not_exists(#s) OR last_refresh < :cutoff",
            ExpressionAttributeNames={"#s": "sub"},
            ExpressionAttributeValues={":cutoff": now - _REFRESH_COOLDOWN_S},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False  # within cooldown -> throttled
        logger.warning("refresh-slot guard write failed; throttling to be safe")
        return False
    except Exception:
        logger.warning("refresh-slot guard unexpected error; throttling to be safe")
        return False


def _me_refresh(sub):
    if not sub:
        return _me_budget(sub)
    if not _acquire_refresh_slot(sub):
        # Within cooldown (or guard unavailable) -- return current budget without re-invoking.
        return _me_budget(sub)
    # Synchronous so the developer sees updated numbers on return (CloudWatch metrics may still
    # lag a minute or two behind the actual request, so this reflects "as of now" metrics).
    try:
        _invoke_aggregator(sync=True)
    except Exception:
        logger.exception("aggregator invoke failed on /me/refresh")
    return _me_budget(sub)


def _scan_config():
    """Return (customer_projects, developers) from the Config table in one scan.

    customer_projects: {cproj_id -> {name, group, cost_center, thresholds, tier1/2/3_model}}
    developers:        [{sub, email, mantle_project_id, customer_project, group, cost_center,
                         thresholds, tier1/2/3_model}]
    """
    customer_projects = {}
    developers = []
    scan = {}
    while True:
        resp = _config.scan(**scan)
        for it in resp.get("Items", []):
            sub = it.get("sub", "")
            if sub.startswith("__project__:"):
                customer_projects[it["project_id"]] = {
                    "name": it.get("name", it["project_id"]),
                    "group": it.get("group", "unknown"),
                    "cost_center": it.get("cost_center", "unknown"),
                    "daily_budget_usd": float(it.get("daily_budget_usd", 0) or 0),
                    "thresholds": it.get("thresholds") or [75, 90, 100],
                    "tier1_model": it.get("tier1_model"),
                    "tier2_model": it.get("tier2_model"),
                    "tier3_model": it.get("tier3_model"),
                }
            elif it.get("project_id"):
                developers.append({
                    "sub": sub,
                    "email": it.get("developer_email"),
                    "mantle_project_id": it.get("mantle_project_id") or it.get("project_id"),
                    "customer_project": it.get("customer_project") or it.get("project_id"),
                })
        if "LastEvaluatedKey" in resp:
            scan["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        else:
            break

    # SINGLE SOURCE OF TRUTH (Option B): attach each developer's governance fields (group,
    # cost_center, tier ladder, thresholds, daily_budget) from their customer-project POLICY row,
    # not from a per-developer copy. A project edit is therefore authoritative for all its devs.
    for d in developers:
        pol = customer_projects.get(d["customer_project"], {})
        d["group"] = pol.get("group", "unknown")
        d["cost_center"] = pol.get("cost_center", "unknown")
        d["thresholds"] = pol.get("thresholds") or [75, 90, 100]
        d["tier1_model"] = pol.get("tier1_model")
        d["tier2_model"] = pol.get("tier2_model")
        d["tier3_model"] = pol.get("tier3_model")
        d["daily_budget_usd"] = pol.get("daily_budget_usd", 0)
    return customer_projects, developers


def _all_budget_rows():
    """Return {mantle_project_id -> budget item} for every row in the Budget table."""
    out = {}
    bscan = {}
    while True:
        resp = _budget.scan(**bscan)
        for b in resp.get("Items", []):
            out[b["project_id"]] = b
        if "LastEvaluatedKey" in resp:
            bscan["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        else:
            break
    return out


def _usage_rollup():
    """Current-day token cost, rolled up per CUSTOMER project (summing each project's
    developers' per-developer mantle budget rows), plus by group / cost_center, plus the
    per-developer detail. Budget rows are keyed by the developer's own mantle project id.
    """
    customer_projects, developers = _scan_config()
    budgets = _all_budget_rows()

    per_project = {}   # cproj_id -> aggregated
    per_developer = []
    by_group = {}
    by_cost_center = {}
    total_cost = 0.0

    total_cache_savings = 0.0

    for cproj_id, meta in customer_projects.items():
        per_project[cproj_id] = {
            "customer_project": cproj_id, "name": meta["name"],
            "group": meta["group"], "cost_center": meta["cost_center"],
            "cost_today_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "cache_savings_usd": 0.0,
            "developers": 0, "budget_exceeded": 0,
        }

    for d in developers:
        b = budgets.get(d["mantle_project_id"], {})
        cost = float(b.get("cost_today_usd", 0) or 0)
        in_tok = int(b.get("input_tokens", 0) or 0)
        out_tok = int(b.get("output_tokens", 0) or 0)
        cache_read = int(b.get("cache_read_tokens", 0) or 0)
        cache_write = int(b.get("cache_write_tokens", 0) or 0)
        cache_savings = float(b.get("cache_savings_usd", 0) or 0)
        exceeded = bool(b.get("budget_exceeded", False))
        per_developer.append({
            "developer": d["email"] or d["sub"],
            "customer_project": d["customer_project"],
            "customer_project_name": customer_projects.get(d["customer_project"], {}).get("name", d["customer_project"]),
            "mantle_project_id": d["mantle_project_id"],
            "group": d["group"], "cost_center": d["cost_center"],
            "cost_today_usd": cost,
            "budget_pct": float(b.get("budget_pct", 0) or 0),
            "budget_exceeded": exceeded,
            "input_tokens": in_tok, "output_tokens": out_tok,
            "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
            "cache_savings_usd": round(cache_savings, 4),
        })
        agg = per_project.get(d["customer_project"])
        if agg is not None:
            agg["cost_today_usd"] += cost
            agg["input_tokens"] += in_tok
            agg["output_tokens"] += out_tok
            agg["cache_read_tokens"] += cache_read
            agg["cache_write_tokens"] += cache_write
            agg["cache_savings_usd"] += cache_savings
            agg["developers"] += 1
            agg["budget_exceeded"] += 1 if exceeded else 0
        by_group[d["group"]] = by_group.get(d["group"], 0.0) + cost
        by_cost_center[d["cost_center"]] = by_cost_center.get(d["cost_center"], 0.0) + cost
        total_cost += cost
        total_cache_savings += cache_savings

    projects = sorted(per_project.values(), key=lambda r: r["cost_today_usd"], reverse=True)
    for p in projects:
        p["cost_today_usd"] = round(p["cost_today_usd"], 4)
        p["cache_savings_usd"] = round(p["cache_savings_usd"], 4)

    return _resp(200, {
        "total_cost_today_usd": round(total_cost, 4),
        "total_cache_savings_usd": round(total_cache_savings, 4),
        "projects": projects,
        "developers": sorted(per_developer, key=lambda r: r["cost_today_usd"], reverse=True),
        "by_group": [{"group": k, "cost_today_usd": round(v, 4)} for k, v in sorted(by_group.items())],
        "by_cost_center": [{"cost_center": k, "cost_today_usd": round(v, 4)} for k, v in sorted(by_cost_center.items())],
    })


def _tier_for_pct(thresholds, pct):
    t = thresholds or [75, 90, 100]
    try:
        lo, mid, top = float(t[0]), float(t[1]), float(t[2])
    except (IndexError, ValueError, TypeError):
        lo, mid, top = 75, 90, 100
    if pct >= top:
        return "blocked"
    if pct >= mid:
        return "tier3"
    if pct >= lo:
        return "tier2"
    return "tier1"


def _enforcement_status():
    """Per-developer enforcement state. Each developer's tier is derived from THEIR OWN
    per-developer budget row (keyed by their mantle project id), against the customer project's
    thresholds/ladder that was copied onto their Config row. Labeled with the customer project.
    """
    customer_projects, developers = _scan_config()
    budgets = _all_budget_rows()
    rows = []
    for d in developers:
        budget = budgets.get(d["mantle_project_id"], {})
        pct = float(budget.get("budget_pct", 0) or 0)
        tier = _tier_for_pct(d["thresholds"], pct)
        tier_model = {
            "tier1": d["tier1_model"], "tier2": d["tier2_model"],
            "tier3": d["tier3_model"], "blocked": None,
        }.get(tier)
        rows.append({
            "developer": d["email"] or d["sub"],
            "customer_project": d["customer_project"],
            "customer_project_name": customer_projects.get(d["customer_project"], {}).get("name", d["customer_project"]),
            "mantle_project_id": d["mantle_project_id"],
            "group": d["group"],
            "cost_center": d["cost_center"],
            "budget_pct": pct,
            "state": tier,
            "resolved_model": tier_model,
            "blocked": tier == "blocked",
        })
    return _resp(200, {"enforcement": sorted(rows, key=lambda r: r["budget_pct"], reverse=True)})


def _alerts():
    """Alerts derived on-the-fly from per-developer budget thresholds (no separate store).

    One alert per developer whose own budget is >= 75%, labeled with the developer + their
    customer project. (Budget rows are per-developer, keyed by mantle project id.)
    """
    customer_projects, developers = _scan_config()
    budgets = _all_budget_rows()
    alerts = []
    for d in developers:
        b = budgets.get(d["mantle_project_id"], {})
        pct = float(b.get("budget_pct", 0) or 0)
        if pct < 75:
            continue
        alerts.append({
            "developer": d["email"] or d["sub"],
            "customer_project": d["customer_project"],
            "customer_project_name": customer_projects.get(d["customer_project"], {}).get("name", d["customer_project"]),
            "group": d["group"],
            "cost_center": d["cost_center"],
            "budget_pct": pct,
            "cost_today_usd": float(b.get("cost_today_usd", 0) or 0),
            "type": "BLOCKED" if pct >= 100 else "WARNING",
            "updated_at": b.get("updated_at"),
        })
    return _resp(200, {"alerts": sorted(alerts, key=lambda a: a["budget_pct"], reverse=True)})


# --------------------------------------------------------------------------- developer ops
def _policy_for(cfg):
    """The developer's customer-project POLICY row (single source of truth for budget/tier/
    thresholds/group/cost_center). Falls back to the dev row itself if the policy is missing."""
    cproj = cfg.get("customer_project")
    if cproj:
        pol = _config.get_item(Key={"sub": f"__project__:{cproj}"}).get("Item")
        if pol:
            return pol
    return cfg


def _me_budget(sub):
    cfg = _config.get_item(Key={"sub": sub}).get("Item")
    if not cfg:
        return _resp(200, {"assigned": False})
    pol = _policy_for(cfg)
    budget = _budget.get_item(Key={"project_id": cfg["project_id"]}).get("Item") or {}
    # daily budget comes from the policy (source of truth); the aggregator also computes % vs it.
    daily = float(pol.get("daily_budget_usd", budget.get("daily_budget_usd", 0)) or 0)
    return _resp(200, {
        "assigned": True,
        "project_id": cfg["project_id"],
        "budget_pct": float(budget.get("budget_pct", 0) or 0),
        "budget_exceeded": bool(budget.get("budget_exceeded", False)),
        "cost_today_usd": float(budget.get("cost_today_usd", 0) or 0),
        "daily_budget_usd": daily,
        "input_tokens": int(budget.get("input_tokens", 0) or 0),
        "output_tokens": int(budget.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(budget.get("cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(budget.get("cache_write_tokens", 0) or 0),
        "cache_savings_usd": round(float(budget.get("cache_savings_usd", 0) or 0), 4),
    })


def _me_config(sub):
    cfg = _config.get_item(Key={"sub": sub}).get("Item")
    if not cfg:
        return _resp(200, {"assigned": False})
    pol = _policy_for(cfg)
    return _resp(200, {
        "assigned": True,
        "project_id": cfg["project_id"],
        "group": pol.get("group"),
        "cost_center": pol.get("cost_center"),
        "tier1_model": pol.get("tier1_model"),
        "tier2_model": pol.get("tier2_model"),
        "tier3_model": pol.get("tier3_model"),
        "thresholds": pol.get("thresholds") or [75, 90, 100],
        "daily_budget_usd": float(pol.get("daily_budget_usd", 0) or 0),
    })


# --------------------------------------------------------------------------- mantle project
_MANTLE_HOST = f"bedrock-mantle.{REGION}.api.aws"


def _mantle_request(method, path, body):
    """SigV4-signed request to the bedrock-mantle Projects REST API (no boto3 client exists).

    The path is built from a hardcoded host + caller-controlled project id path segment; all
    mutable data goes in the BODY. We still hard-validate scheme AND host before opening the
    connection (defense-in-depth: blocks file:// and any non-mantle host).
    """
    url = f"https://{_MANTLE_HOST}{path}"
    payload = json.dumps(body).encode("utf-8")
    creds = _session.get_credentials().get_frozen_credentials()
    req = AWSRequest(method=method, url=url, data=payload,
                     headers={"Host": _MANTLE_HOST, "Content-Type": "application/json"})
    SigV4Auth(creds, "bedrock-mantle", REGION).add_auth(req)
    prepared = req.prepare()
    parsed = urlparse(prepared.url)
    if parsed.scheme != "https" or parsed.hostname != _MANTLE_HOST:
        raise ValueError("mantle URL failed scheme/host validation")
    r = urllib.request.Request(prepared.url, data=payload, method=method)  # nosec B310 # nosemgrep: dynamic-urllib-use-detected - HTTPS+host validated; hardcoded host template
    for k, v in prepared.headers.items():
        r.add_header(k, v)
    with urllib.request.urlopen(r, timeout=20) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected - HTTPS+host validated; hardcoded host template
        return json.load(resp)


def _create_mantle_project(name, tags):
    data = _mantle_request("POST", "/v1/organization/projects", {"name": name, "tags": tags})
    return data["id"]


def _update_mantle_project_tags(project_id, tags):
    """Re-tag an existing developer mantle project (used on reassignment). Verified live:
    POST /v1/organization/projects/{id} with add_tags updates existing tag values in place and
    adds any new keys. project_id is a mantle-issued id (proj_...), not user free-text."""
    return _mantle_request("POST", f"/v1/organization/projects/{project_id}",
                           {"add_tags": tags})


def _num(v):
    from decimal import Decimal
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _pos_num(v):
    """Coerce to a NON-NEGATIVE Decimal. A negative daily_budget_usd previously disabled budget
    enforcement (the aggregator forced budget_pct=0), so we clamp negatives to 0 here; the
    aggregator then treats 0 as the global-default budget, keeping enforcement on."""
    from decimal import Decimal
    n = _num(v)
    return n if n >= 0 else Decimal("0")




# --------------------------------------------------------------------------- analytics (S3 direct)
# GET /admin/analytics?days=7|30|90&dim=trend|cost_center|group|model|developer
#
# Reads the enriched metric lake directly from S3 (s3://<analytics>/mantle/YYYY/MM/DD/HH/*.gz).
# The data volume is small (a handful of GZIP-JSON objects per hour), so a direct scan of the
# day-range prefixes is fast and avoids the Athena/Glue/Lake-Formation dependency entirely.
#
# CloudWatch metric-stream emits every dimension permutation, so we select a single canonical
# slice per aggregation to avoid double counting:
#   - tokens by project/cost_center/group/developer  -> project-level metrics
#       (TotalInputTokens / TotalOutputTokens) where Project is set and Model is NULL; these
#       carry the enrichment tags CostCenter / Group / Developer.
#   - tokens by model                                -> per-model metrics
#       (InputTokens / OutputTokens) where both Model and Project are set.
# token count = value.sum. Spend is per-model from the pricing table (blended in/out) with the
# same conservative fallback the aggregator uses (0.015 in / 0.075 out per 1k) so figures reconcile.

_ALLOWED_DAYS = {"1", "7", "30", "90"}
_ALLOWED_DIMS = {"trend", "cost_center", "group", "model", "developer"}
_FALLBACK_IN = 0.015
_FALLBACK_OUT = 0.075
# Cache-rate fallback ratios (of the model's input rate), matching gw_pricing_refresh and the
# aggregator: cache reads are billed cheaper than input, cache writes at a premium.
_CACHE_READ_RATIO_FALLBACK = 0.1
_CACHE_WRITE_RATIO_FALLBACK = 1.25

ANALYTICS_BUCKET = os.environ.get("ANALYTICS_BUCKET", "")
_s3 = boto3.client("s3")


def _pricing_map():
    """model_id -> (input_per_1k, output_per_1k, cache_read_per_1k, cache_write_per_1k).

    Scans the small pricing table once. Cache rates use the stored columns when present,
    otherwise the input-rate fallback ratios (so cache spend + savings reconcile with the
    aggregator's budget math)."""
    out = {}
    try:
        resp = _pricing.scan(ProjectionExpression=(
            "model_id, input_price_per_1k, output_price_per_1k, price_available, "
            "cache_read_price_per_1k, cache_write_price_per_1k"
        ))
        for it in resp.get("Items", []):
            if it.get("price_available"):
                inp = float(it.get("input_price_per_1k", 0) or 0)
                outp = float(it.get("output_price_per_1k", 0) or 0)
                cread = (
                    float(it["cache_read_price_per_1k"])
                    if it.get("cache_read_price_per_1k") is not None
                    else inp * _CACHE_READ_RATIO_FALLBACK
                )
                cwrite = (
                    float(it["cache_write_price_per_1k"])
                    if it.get("cache_write_price_per_1k") is not None
                    else inp * _CACHE_WRITE_RATIO_FALLBACK
                )
                out[it["model_id"]] = (inp, outp, cread, cwrite)
    except Exception:
        logger.warning("pricing scan failed; using fallback rates only")
    return out


def _iter_metric_records(days):
    """Yield enriched metric dicts from S3 for the last `days` days (inclusive of today, UTC).

    Lists only the mantle/YYYY/MM/DD/ prefixes in range (cheap prefix listing), downloads each
    .gz object, and parses newline-delimited JSON. Robust to empty/partial objects.
    """
    import datetime
    import gzip

    today = datetime.datetime.now(datetime.timezone.utc).date()
    prefixes = []
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        prefixes.append(f"mantle/{d.year:04d}/{d.month:02d}/{d.day:02d}/")

    paginator = _s3.get_paginator("list_objects_v2")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=ANALYTICS_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".gz"):
                    continue
                try:
                    body = _s3.get_object(Bucket=ANALYTICS_BUCKET, Key=key)["Body"].read()
                    text = gzip.decompress(body).decode("utf-8")
                except Exception:
                    logger.warning("skipping unreadable analytics object %s", key)
                    continue
                for line in text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue


def _rec_day(rec):
    import datetime
    ts = rec.get("timestamp")
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts / 1000.0, datetime.timezone.utc).date().isoformat()


def _analytics(qs):
    if not ANALYTICS_BUCKET:
        return _resp(503, {"error": "analytics not configured"})
    days = qs.get("days", "30")
    dim = qs.get("dim", "trend")
    if days not in _ALLOWED_DAYS:
        days = "30"
    if dim not in _ALLOWED_DIMS:
        return _resp(400, {"error": f"dim must be one of {sorted(_ALLOWED_DIMS)}"})
    days_i = int(days)

    try:
        if dim == "model":
            # Per-model in/out + cache tokens (Model + Project set) -> priced spend + savings.
            agg = {}  # model -> {input, output, cache_read, cache_write}
            for rec in _iter_metric_records(days_i):
                dims = rec.get("dimensions") or {}
                model = dims.get("Model")
                if not model or not dims.get("Project"):
                    continue
                mn = rec.get("metric_name")
                if mn not in ("InputTokens", "OutputTokens",
                              "CacheReadInputTokens", "CacheWriteInputTokens"):
                    continue
                tok = (rec.get("value") or {}).get("sum") or 0
                e = agg.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
                if mn == "InputTokens":
                    e["input"] += tok
                elif mn == "OutputTokens":
                    e["output"] += tok
                elif mn == "CacheReadInputTokens":
                    e["cache_read"] += tok
                else:
                    e["cache_write"] += tok
            prices = _pricing_map()
            data = []
            for m, e in agg.items():
                pin, pout, cread, cwrite = prices.get(
                    m, (_FALLBACK_IN, _FALLBACK_OUT,
                        _FALLBACK_IN * _CACHE_READ_RATIO_FALLBACK,
                        _FALLBACK_IN * _CACHE_WRITE_RATIO_FALLBACK))
                cost = (e["input"] / 1000.0 * pin + e["output"] / 1000.0 * pout
                        + e["cache_read"] / 1000.0 * cread + e["cache_write"] / 1000.0 * cwrite)
                savings = e["cache_read"] / 1000.0 * (pin - cread)
                data.append({
                    "model": m,
                    "input_tokens": int(e["input"]),
                    "output_tokens": int(e["output"]),
                    "cache_read_tokens": int(e["cache_read"]),
                    "cache_write_tokens": int(e["cache_write"]),
                    "cache_savings_usd": round(max(savings, 0.0), 4),
                    "cost_usd": round(cost, 4),
                    "priced": m in prices,
                })
            data.sort(key=lambda x: x["cost_usd"], reverse=True)
            return _resp(200, {"dim": dim, "days": days_i, "data": data})

        # Tag-based dims + trend use the project-level slice (Project set, Model NULL) which
        # carries the enrichment tags.
        tag_key = {"cost_center": "CostCenter", "group": "Group", "developer": "Developer"}

        if dim in tag_key:
            key_field = tag_key[dim]
            agg = {}  # key -> {input, output, cache_read, cache_write}
            for rec in _iter_metric_records(days_i):
                dims = rec.get("dimensions") or {}
                if not dims.get("Project"):
                    continue
                mn = rec.get("metric_name")
                has_model = bool(dims.get("Model"))
                # Input/output come from the project-level slice (Model NULL). Cache metrics only
                # exist at Project+Model granularity, so read those from the per-model slice.
                is_totals = (not has_model) and mn in ("TotalInputTokens", "TotalOutputTokens")
                is_cache = has_model and mn in ("CacheReadInputTokens", "CacheWriteInputTokens")
                if not (is_totals or is_cache):
                    continue
                k = rec.get(key_field) or "unknown"
                tok = (rec.get("value") or {}).get("sum") or 0
                e = agg.setdefault(k, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
                if mn == "TotalInputTokens":
                    e["input"] += tok
                elif mn == "TotalOutputTokens":
                    e["output"] += tok
                elif mn == "CacheReadInputTokens":
                    e["cache_read"] += tok
                else:
                    e["cache_write"] += tok
            data = []
            for k, e in agg.items():
                cread = _FALLBACK_IN * _CACHE_READ_RATIO_FALLBACK
                cwrite = _FALLBACK_IN * _CACHE_WRITE_RATIO_FALLBACK
                cost = (e["input"] / 1000.0 * _FALLBACK_IN + e["output"] / 1000.0 * _FALLBACK_OUT
                        + e["cache_read"] / 1000.0 * cread + e["cache_write"] / 1000.0 * cwrite)
                savings = e["cache_read"] / 1000.0 * (_FALLBACK_IN - cread)
                data.append({
                    "key": k,
                    "input_tokens": int(e["input"]),
                    "output_tokens": int(e["output"]),
                    "cache_read_tokens": int(e["cache_read"]),
                    "cache_write_tokens": int(e["cache_write"]),
                    "cache_savings_usd": round(max(savings, 0.0), 4),
                    "cost_usd": round(cost, 4),
                })
            data.sort(key=lambda x: x["cost_usd"], reverse=True)
            return _resp(200, {"dim": dim, "days": days_i, "data": data})

        # trend: tokens per day. Input/output from the project-level slice (Model NULL); cache
        # from the per-model slice (Project+Model). Priced with the blended fallback + cache ratios.
        agg = {}  # day -> {input, output, cache_read, cache_write}
        for rec in _iter_metric_records(days_i):
            dims = rec.get("dimensions") or {}
            if not dims.get("Project"):
                continue
            mn = rec.get("metric_name")
            has_model = bool(dims.get("Model"))
            is_totals = (not has_model) and mn in ("TotalInputTokens", "TotalOutputTokens")
            is_cache = has_model and mn in ("CacheReadInputTokens", "CacheWriteInputTokens")
            if not (is_totals or is_cache):
                continue
            day = _rec_day(rec)
            if not day:
                continue
            tok = (rec.get("value") or {}).get("sum") or 0
            e = agg.setdefault(day, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
            if mn == "TotalInputTokens":
                e["input"] += tok
            elif mn == "TotalOutputTokens":
                e["output"] += tok
            elif mn == "CacheReadInputTokens":
                e["cache_read"] += tok
            else:
                e["cache_write"] += tok
        data = []
        cread = _FALLBACK_IN * _CACHE_READ_RATIO_FALLBACK
        cwrite = _FALLBACK_IN * _CACHE_WRITE_RATIO_FALLBACK
        for day in sorted(agg):
            e = agg[day]
            cost = (e["input"] / 1000.0 * _FALLBACK_IN + e["output"] / 1000.0 * _FALLBACK_OUT
                    + e["cache_read"] / 1000.0 * cread + e["cache_write"] / 1000.0 * cwrite)
            savings = e["cache_read"] / 1000.0 * (_FALLBACK_IN - cread)
            data.append({
                "day": day,
                "input_tokens": int(e["input"]),
                "output_tokens": int(e["output"]),
                "cache_read_tokens": int(e["cache_read"]),
                "cache_write_tokens": int(e["cache_write"]),
                "cache_savings_usd": round(max(savings, 0.0), 4),
                "cost_usd": round(cost, 4),
            })
        return _resp(200, {"dim": dim, "days": days_i, "data": data})
    except Exception:
        # Log the full detail server-side (stack trace + message) but return a generic message.
        # Raw exception text can disclose internal identifiers (bucket keys, ARNs, table names)
        # to the client, so we never echo `e` back in the response body.
        logger.exception("analytics query failed")
        return _resp(500, {"error": "analytics query failed"})
