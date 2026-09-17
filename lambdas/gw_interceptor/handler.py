# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tokenomics request interceptor (AgentCore Gateway REQUEST interceptor).

Runs BEFORE the gateway calls the bedrock-mantle target and BEFORE Cedar policy evaluation.
For an inference target the gateway passes the HTTP payload with a base64-encoded body.

Responsibilities (all after gateway JWT auth has already succeeded):
  1. Identify the developer  -> decode JWT `sub` from the Authorization header.
  2. Look up the developer's project + tier config (Config table, key = sub).
  3. Look up the project's current budget state (Budget table, key = project_id).
  4. Inject the OpenAI-Project header so mantle attributes usage to the developer's project.
  5. Resolve the model:
        - The IDE normally sends only the virtual model alias -> resolve to the project's
          tier model for the current budget %.
        - Safety guard: any other (concrete) model that is NOT in the project's allowed_models
          is ALSO rewritten to the tier model, so a misconfigured/rogue IDE stays governed.
        - A concrete model that IS in allowed_models passes through unchanged.
  6. Inject budget_exceeded into the body so a static Cedar policy can hard-block at 100%.
  7. (optional) Fast-path 403: if ENFORCE_MODE=interceptor, return a 403 response directly
     when over budget, so the model is never called.

Security notes:
  - JWT is NOT re-verified here (the gateway already validated it); we only base64url-decode
    the payload to read claims. We never log the raw token.
  - DynamoDB access is scoped to the two specific tables via the execution role.
"""
import base64
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONFIG_TABLE = os.environ["CONFIG_TABLE"]
BUDGET_TABLE = os.environ["BUDGET_TABLE"]
# Optional: correlation table for per-project cache metering. The RESPONSE interceptor
# (gw_cache_meter) cannot see the request on an HTTP/inference target (gatewayRequest is null),
# so it resolves the project by the shared REQUEST_ID that both interceptors receive in the
# Lambda client context. We stamp REQUEST_ID -> project_id + model here (short TTL) and the
# cache meter reads it back. Absent env -> feature disabled (no-op), so this stays backward
# compatible. https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
REQMAP_TABLE = os.environ.get("REQMAP_TABLE")
REQMAP_TTL_SECONDS = int(os.environ.get("REQMAP_TTL_SECONDS", "900"))  # 15 min
VIRTUAL_MODEL = os.environ.get("VIRTUAL_MODEL", "team-coding-model")
TARGET_PREFIX = os.environ.get("TARGET_PREFIX", "bedrock-models")
ENFORCE_MODE = os.environ.get("ENFORCE_MODE", "cedar").lower()  # "cedar" | "interceptor"
# When True, an unmapped developer is allowed through unchanged. When False (default), an
# unmapped developer is blocked (fail-closed) -- no unassigned / ungoverned access.
FAIL_OPEN = os.environ.get("FAIL_OPEN", "false").lower() == "true"

_dynamodb = boto3.resource("dynamodb")
_config_table = _dynamodb.Table(CONFIG_TABLE)
_budget_table = _dynamodb.Table(BUDGET_TABLE)
_reqmap_table = _dynamodb.Table(REQMAP_TABLE) if REQMAP_TABLE else None


# ---------------------------------------------------------------------------
# JWT (payload-only decode; signature already verified by the gateway)
# ---------------------------------------------------------------------------
def _decode_jwt_claims(auth_header: str) -> dict:
    if not auth_header:
        return {}
    token = auth_header.split(" ", 1)[1] if " " in auth_header else auth_header
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # pad base64url
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        logger.warning("Failed to decode JWT payload")
        return {}


def _get_header(headers: dict, name: str) -> str:
    lname = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == lname:
            return v
    return ""


def _strip_headers(headers: dict, *names) -> None:
    """Remove ALL case-variants of the given header names from `headers` in place. HTTP header
    names are case-insensitive, so a client could smuggle `Anthropic-Workspace-Id` or
    `openai-project` (any case) to spoof cost attribution; a single fixed-case pop would miss
    those. This scrubs every variant so the interceptor is the sole authority on attribution."""
    targets = {n.lower() for n in names}
    for k in [k for k in list(headers.keys()) if k.lower() in targets]:
        headers.pop(k, None)


def _is_anthropic_format(req: dict, headers: dict) -> bool:
    """True ONLY when the request uses the Anthropic Messages API, which the gateway/mantle serves
    on the /v1/messages path. Classification is PATH-AUTHORITATIVE and never falls back to a
    client-supplied header: the chosen attribution header (`anthropic-workspace-id` vs
    `OpenAI-Project`) determines which developer's budget is charged, so letting the client steer
    it would be a cost-attribution/governance bypass. Every non-/v1/messages inference route
    (OpenAI Chat Completions /v1/chat/completions AND the OpenAI Responses API /v1/responses, plus
    any unknown path) is treated as OpenAI-format (`OpenAI-Project`) -- mantle only serves the
    Anthropic protocol on /v1/messages, so anything else must not receive Anthropic attribution.

    NOTE: `headers` is intentionally unused now (kept in the signature for call-site stability);
    the anthropic-version header must NOT influence attribution."""
    path = (req.get("path") or "").lower()
    return path.endswith("/v1/messages") or path.endswith("/messages")


def _is_chat_completions_format(req: dict) -> bool:
    """True ONLY for the OpenAI Chat Completions route (/v1/chat/completions). This is the only
    route where we may inject the `budget_exceeded` flag into the request body for the Cedar
    policy to read. The OpenAI Responses API (/v1/responses) uses a different, stricter body
    schema ({input, max_output_tokens, ...}) that rejects unknown fields with an
    internal_server_error (500), so we must NOT add anything to its body. Anthropic Messages
    likewise rejects unknown body fields."""
    path = (req.get("path") or "").lower()
    return path.endswith("/v1/chat/completions") or path.endswith("/chat/completions")


# ---------------------------------------------------------------------------
# Config / budget lookups
# ---------------------------------------------------------------------------
def _get_developer_config(sub: str) -> dict | None:
    """Resolve the developer's effective config. Identity fields (project_id = the developer's
    own mantle project, customer_project) come from the developer row; the tier ladder +
    thresholds + allowed_models are the SINGLE SOURCE OF TRUTH on the customer-project POLICY
    row (`__project__:<customer_project>`) and are merged in here -- so a project edit in the
    admin console governs every developer with no per-developer drift.
    """
    if not sub:
        return None
    try:
        dev = _config_table.get_item(Key={"sub": sub}).get("Item")
    except Exception:
        logger.warning("Config lookup failed for developer")
        return None
    if not dev:
        return None
    cproj = dev.get("customer_project")
    if cproj:
        try:
            pol = _config_table.get_item(Key={"sub": f"__project__:{cproj}"}).get("Item")
        except Exception:
            pol = None
        if pol:
            # Policy is authoritative for governance fields; dev row keeps identity fields.
            for f in ("tier1_model", "tier2_model", "tier3_model", "thresholds",
                      "allowed_models", "daily_budget_usd"):
                if pol.get(f) is not None:
                    dev[f] = pol[f]
    return dev


def _get_budget_state(project_id: str):
    """Return (budget_item, ok). ok=False signals the DynamoDB read FAILED (as opposed to a
    missing row, which returns ({}, True)). Callers fail CLOSED on ok=False -- we must not treat
    an unreadable budget as "0% used / allowed"."""
    try:
        resp = _budget_table.get_item(Key={"project_id": project_id})
        return (resp.get("Item") or {}), True
    except Exception:
        logger.warning("Budget lookup failed for project")
        return {}, False


def _budget_is_fresh(budget: dict) -> bool:
    """True only when the budget row is for the current UTC day. A stale row (yesterday's, or one
    not yet reset after midnight) must not be trusted to grant access -- freshness is required so
    an over-budget state can't be laundered by an outdated row, and a stale under-budget row can't
    grant tier1 indefinitely."""
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return budget.get("date") == today


# ---------------------------------------------------------------------------
# REQUEST_ID -> project correlation (for the cache-metering RESPONSE interceptor)
# ---------------------------------------------------------------------------
def _request_id(context) -> str:
    """Read the gateway REQUEST_ID from the Lambda client context.

    For HTTP targets the gateway passes request metadata (GATEWAY_ARN, GATEWAY_ACCOUNT_ID,
    REQUEST_ID, SOURCE_IP) via the invocation's client context; REQUEST_ID is always present.
    The same REQUEST_ID is delivered to BOTH the request and response interceptor invocations,
    which is what lets the response interceptor attribute cache tokens to this project.
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
    """
    try:
        custom = getattr(context, "client_context", None)
        custom = getattr(custom, "custom", None) or {}
        return custom.get("REQUEST_ID") or ""
    except Exception:
        return ""


def _stamp_reqmap(context, project_id: str, model: str) -> None:
    """Best-effort: record REQUEST_ID -> project_id + model so the cache meter can attribute
    cache tokens per project. NEVER let a mapping failure affect the actual request."""
    if not _reqmap_table:
        return
    request_id = _request_id(context)
    if not request_id or not project_id:
        return
    try:
        _reqmap_table.put_item(Item={
            "request_id": request_id,
            "project_id": project_id,
            "model": _normalize(model) if model else "",
            "expires_at": int(time.time()) + REQMAP_TTL_SECONDS,  # DynamoDB TTL (epoch seconds)
        })
    except Exception:
        logger.warning("Failed to stamp REQUEST_ID->project mapping (cache metering only)")


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------
def _tier_model(config: dict, budget_pct: float) -> str:
    """Pick the real model for the current budget tier from per-project config.

    thresholds default to [75, 90, 100]. Below thresholds[0] -> tier1; between [0] and [1]
    -> tier2; at/above [1] -> tier3. Enforcement of the 100% hard stop is done by
    Cedar/interceptor, not by model choice.
    """
    thresholds = config.get("thresholds") or [75, 90, 100]
    t1, t2, t3 = config.get("tier1_model"), config.get("tier2_model"), config.get("tier3_model")
    try:
        lo, mid = float(thresholds[0]), float(thresholds[1])
    except (IndexError, ValueError, TypeError):
        lo, mid = 75.0, 90.0
    if budget_pct >= mid:
        return t3 or t2 or t1
    if budget_pct >= lo:
        return t2 or t1
    return t1


def _qualify(model: str) -> str:
    """Ensure the model id is target-qualified, e.g. 'bedrock-models/deepseek.v3.2'."""
    if not model:
        return model
    return model if "/" in model else f"{TARGET_PREFIX}/{model}"


def _normalize(model: str) -> str:
    """Strip the target prefix for comparison against the allowed_models list."""
    return model.split("/", 1)[1] if model and "/" in model else (model or "")


def resolve_model(config: dict, requested_model: str, budget_pct: float) -> str:
    """Resolve the model that will actually be served.

    - Virtual alias -> the project's current tier model.
    - Concrete model in allowed_models -> passes through (qualified).
    - Any other concrete model -> SAFETY GUARD: rewritten to the tier model, so a
      misconfigured or rogue IDE cannot escape governance (option a).
    """
    tier = _qualify(_tier_model(config, budget_pct))
    if requested_model == VIRTUAL_MODEL or not requested_model:
        return tier
    allowed = {_normalize(m) for m in (config.get("allowed_models") or [])}
    if _normalize(requested_model) in allowed:
        return _qualify(requested_model)
    logger.info("Requested model not in project's allowed set; rewriting to tier model")
    return tier


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event, context):
    http = event.get("http", {})
    req = http.get("gatewayRequest", {})
    headers = dict(req.get("headers", {}) or {})
    body_b64 = req.get("body")

    claims = _decode_jwt_claims(_get_header(req.get("headers", {}), "authorization"))
    sub = claims.get("sub")

    config = _get_developer_config(sub)

    # Unmapped developer -> block unless FAIL_OPEN. No unassigned / ungoverned access.
    if not config:
        logger.info("No config for developer (has_sub=%s); fail_open=%s", bool(sub), FAIL_OPEN)
        if not FAIL_OPEN:
            return _deny_response("No project assigned to this identity. Contact your administrator.")
        return _passthrough(headers, body_b64)

    project_id = config["project_id"]
    budget, budget_ok = _get_budget_state(project_id)
    # FAIL-CLOSED: if the budget read errored, we cannot confirm the developer is under budget,
    # so deny rather than allow ungoverned spend. (Set FAIL_OPEN=true to override for debugging.)
    if not budget_ok and not FAIL_OPEN:
        logger.info("Interceptor 403 (budget unreadable; fail-closed) project=%s", project_id)
        return _deny_response("Budget state temporarily unavailable. Request blocked.")
    # FAIL-CLOSED on freshness: a missing row or a stale row (not today's UTC date) means we cannot
    # prove the developer is under today's budget, so treat it as exceeded. The aggregator writes a
    # fresh row every ~5 min; a legitimately-new project gets its first row on the next sweep.
    budget_pct = float(budget.get("budget_pct", 0) or 0)
    if not _budget_is_fresh(budget):
        logger.info("Budget row missing/stale project=%s -> over budget (fail-closed)", project_id)
        budget_exceeded = True
    else:
        budget_exceeded = bool(budget.get("budget_exceeded", budget_pct >= 100))

    is_anthropic = _is_anthropic_format(req, headers)

    # Attribution: mantle attributes usage to this project. The header name depends on the API
    # format -- the Anthropic (/v1/messages) route uses `anthropic-workspace-id`, the OpenAI
    # (/v1/chat/completions) route uses `OpenAI-Project`. Sending the wrong one 400s. Both point
    # at the same per-developer project id, so metering lands on the same CloudWatch Project.
    #
    # SECURITY: scrub EVERY case-variant of BOTH attribution headers the client may have sent
    # before we set the authoritative one. Header names are case-insensitive, so a client could
    # otherwise smuggle `Anthropic-Workspace-Id`/`openai-project` (any case) to attribute their
    # spend to another project and evade per-developer cost tracking. The interceptor is the sole
    # source of truth for attribution.
    _strip_headers(headers, "anthropic-workspace-id", "OpenAI-Project")
    if is_anthropic:
        headers["anthropic-workspace-id"] = project_id
    else:
        headers["OpenAI-Project"] = project_id

    # Budget enforcement. Only the Chat Completions route can carry the `budget_exceeded` flag
    # in its body for the static Cedar policy to read. The other two routes reject unknown body
    # fields, so we enforce the hard stop directly in the interceptor for them:
    #  - Chat Completions (/v1/chat/completions): inject `budget_exceeded` into the body ->
    #    Cedar reads context.input.budget_exceeded and forbids over-budget requests. (Also
    #    honors the optional ENFORCE_MODE=interceptor fast-path.)
    #  - Anthropic Messages (/v1/messages): rejects unknown body fields ("Extra inputs are not
    #    permitted").
    #  - OpenAI Responses (/v1/responses): stricter schema; an unknown field yields a 500
    #    internal_server_error.
    #  For the latter two, block over-budget here (fail-closed at 100%), independent of Cedar.
    is_chat_completions = _is_chat_completions_format(req)
    enforce_in_interceptor = (not is_chat_completions) or ENFORCE_MODE == "interceptor"
    if budget_exceeded and enforce_in_interceptor:
        logger.info("Interceptor 403 (over budget) project=%s route=%s",
                    project_id, "anthropic" if is_anthropic else "openai")
        return _deny_response("Daily token budget exhausted. Request blocked.")

    # Resolve the model. Inject the budget flag into the body ONLY on the Chat Completions route
    # (the only route whose body schema tolerates the extra field).
    out_body = body_b64
    resolved_model = None
    if body_b64:
        try:
            payload = json.loads(base64.b64decode(body_b64))
        except (ValueError, TypeError):
            # FAIL-CLOSED: an unparseable body cannot be governed (no model resolution, no budget
            # flag), so deny rather than forwarding it ungoverned.
            logger.info("Interceptor 403 (unparseable body; fail-closed) project=%s", project_id)
            return _deny_response("Malformed request body. Request blocked.")
        resolved = resolve_model(config, payload.get("model"), budget_pct)
        # FAIL-CLOSED: resolve_model returns None only when the project's tier ladder is unset or
        # its policy row is missing -- i.e. governance cannot be applied. Deny rather than forward
        # the client's requested model verbatim (which would escape the allowlist/tier controls).
        if not resolved:
            logger.info("Interceptor 403 (no governed model; tier ladder unset; fail-closed) project=%s", project_id)
            return _deny_response("No model policy configured for this project. Contact your administrator.")
        payload["model"] = resolved
        resolved_model = resolved
        if is_chat_completions:
            payload["budget_exceeded"] = budget_exceeded  # -> context.input.budget_exceeded
        out_body = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")

    # Record REQUEST_ID -> project + resolved model so the RESPONSE interceptor (cache meter)
    # can attribute cache tokens to this project. Best-effort; never blocks the request.
    _stamp_reqmap(context, project_id, resolved_model)

    result = {"interceptorOutputVersion": "1.0",
              "http": {"transformedGatewayRequest": {"headers": headers}}}
    if out_body is not None:
        result["http"]["transformedGatewayRequest"]["body"] = out_body
    return result


def _passthrough(headers: dict, body_b64):
    out = {"interceptorOutputVersion": "1.0",
           "http": {"transformedGatewayRequest": {"headers": headers}}}
    if body_b64 is not None:
        out["http"]["transformedGatewayRequest"]["body"] = body_b64
    return out


def _deny_response(message: str, status_code: int = 403):
    """Short-circuit: gateway returns this without calling the target."""
    body = base64.b64encode(
        json.dumps(
            {"error": {"type": "budget_exceeded", "code": str(status_code), "message": message}}
        ).encode("utf-8")
    ).decode("utf-8")
    return {"interceptorOutputVersion": "1.0",
            "http": {"transformedGatewayResponse": {
                "contentType": "application/json",
                "statusCode": status_code,
                "headers": {"Content-Type": "application/json"},
                "body": body}}}
