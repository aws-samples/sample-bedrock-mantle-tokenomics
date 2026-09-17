# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tokenomics governance aggregator (EventBridge, every 5 minutes).

For each active project this Lambda:
  1. Reads today's per-model token usage from CloudWatch metrics (AWS/BedrockMantle),
     Stat=Sum over [today midnight UTC -> now]. This is idempotent and self-healing: each
     run recomputes the full daily total from source, so a missed interval only causes
     staleness (a few minutes), never a leak.
  2. Prices each model's tokens with that model's per-1K rate from the Pricing table and
     sums to a blended daily cost (accurate even when a developer was downgraded across
     multiple models during the day).
  3. Overwrites the Budget row for the project with input/output tokens, cost, budget_pct,
     and the budget_exceeded flag (read by the interceptor / static Cedar policy).

Design notes:
  - Reads CloudWatch METRICS (GetMetricData), not logs. Mantle does not emit invocation logs.
  - "Daily reset" is automatic: after midnight the [midnight->now] window returns ~0, so the
    overwrite naturally resets the row. The `date` field lets readers detect a stale row.
  - Projects and their daily_budget_usd + allowed_models are read from the Config table
    (deduplicated by project_id via the by-project GSI).
"""
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONFIG_TABLE = os.environ["CONFIG_TABLE"]
BUDGET_TABLE = os.environ["BUDGET_TABLE"]
PRICING_TABLE = os.environ["PRICING_TABLE"]
PROJECT_INDEX = os.environ.get("PROJECT_INDEX", "by-project")
GLOBAL_DEFAULT_BUDGET_USD = float(os.environ.get("GLOBAL_DEFAULT_BUDGET_USD", "5"))
NAMESPACE = "AWS/BedrockMantle"
# Custom namespace where the cache-meter RESPONSE interceptor emits per-project prompt-cache
# token counts (dims Project+Model, metrics CacheReadInputTokens / CacheWriteInputTokens).
CACHE_NAMESPACE = os.environ.get("CACHE_NAMESPACE", "Tokenomics/Cache")

_dynamodb = boto3.resource("dynamodb")
_config_table = _dynamodb.Table(CONFIG_TABLE)
_budget_table = _dynamodb.Table(BUDGET_TABLE)
_pricing_table = _dynamodb.Table(PRICING_TABLE)
_cloudwatch = boto3.client("cloudwatch")


def lambda_handler(event, context):
    projects = _list_projects()
    if not projects:
        logger.info("No active projects to aggregate")
        return {"statusCode": 200, "body": "no projects"}

    now = datetime.now(timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    updated = 0
    for project_id, meta in projects.items():
        try:
            _aggregate_project(project_id, meta, midnight, now)
            updated += 1
        except Exception:
            logger.warning("Failed to aggregate a project; leaving its prior budget row intact",
                           exc_info=True)
    logger.info("Aggregated %d project(s)", updated)
    return {"statusCode": 200, "body": f"aggregated {updated}"}


def _list_projects() -> dict:
    """Per-developer mantle project_id -> {daily_budget_usd, allowed_models}.

    SINGLE SOURCE OF TRUTH (Option B): the daily budget + allowed models come from the
    customer-project POLICY row (`__project__:<cproj>`), resolved via each developer's
    `customer_project` attribute -- NOT from the (potentially stale) copy on the developer's
    own Config row. Editing a project's budget in the admin console is therefore authoritative
    for every developer assigned to it, with no per-developer drift.
    """
    # First pass: collect policy rows (cproj -> budget/allowed_models) and developer rows.
    policies = {}   # cproj_id -> {daily_budget_usd, allowed_models}
    developers = []  # [{mantle_project_id, customer_project}]
    try:
        scan_kwargs = {}
        while True:
            resp = _config_table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                sub = item.get("sub", "")
                if sub.startswith("__project__:"):
                    cproj = item.get("project_id")
                    if cproj:
                        policies[cproj] = {
                            "daily_budget_usd": float(item.get("daily_budget_usd", 0) or 0)
                            or GLOBAL_DEFAULT_BUDGET_USD,
                            "allowed_models": item.get("allowed_models") or [],
                            # Tier models the interceptor may serve even when NOT in allowed_models.
                            # We must meter these too, or spend on a tier model escapes the budget.
                            "tier_models": [item.get(k) for k in
                                            ("tier1_model", "tier2_model", "tier3_model")
                                            if item.get(k)],
                        }
                    continue
                if sub.startswith("__"):
                    continue  # settings / non-developer rows
                mantle = item.get("project_id")  # per-dev mantle project id (budget row key)
                if mantle:
                    developers.append({
                        "mantle_project_id": mantle,
                        "customer_project": item.get("customer_project"),
                    })
            if "LastEvaluatedKey" in resp:
                scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            else:
                break
    except Exception:
        logger.warning("Failed to list projects from Config table", exc_info=True)
        return {}

    # Second pass: each developer's budget row is computed against its customer project's policy.
    projects = {}
    for d in developers:
        pol = policies.get(d.get("customer_project"), {})
        projects[d["mantle_project_id"]] = {
            "daily_budget_usd": pol.get("daily_budget_usd") or GLOBAL_DEFAULT_BUDGET_USD,
            "allowed_models": pol.get("allowed_models") or [],
            "tier_models": pol.get("tier_models") or [],
        }
    return projects


def _aggregate_project(project_id, meta, midnight, now):
    # Meter the UNION of: allowed_models, the project's tier models (which the interceptor may
    # serve even when not allow-listed), and every model actually seen in CloudWatch today. The
    # models_seen_today discovery is the catch-all -- any model that incurred spend is metered,
    # so spend can no longer escape the budget by using a tier/non-allow-listed model.
    seen = _models_seen_today(project_id, midnight, now)
    union = []
    for m in (list(meta.get("allowed_models") or []) + list(meta.get("tier_models") or []) + list(seen)):
        if m and _normalize(m) not in {_normalize(x) for x in union}:
            union.append(m)
    models = union
    period = _day_period_seconds(midnight, now)

    # Build one GetMetricData request: per-model InputTokens/OutputTokens (Project+Model)
    # plus project-level totals as a cross-check, plus per-model prompt-cache read/write
    # tokens from the custom Tokenomics/Cache namespace.
    queries = []
    id_map = {}
    for i, model in enumerate(models):
        norm = _normalize(model)
        in_id, out_id = f"in{i}", f"out{i}"
        cr_id, cw_id = f"cr{i}", f"cw{i}"
        id_map[in_id] = ("in", norm)
        id_map[out_id] = ("out", norm)
        id_map[cr_id] = ("cache_read", norm)
        id_map[cw_id] = ("cache_write", norm)
        dims = [{"Name": "Project", "Value": project_id}, {"Name": "Model", "Value": norm}]
        queries.append(_q(in_id, "InputTokens", dims, period))
        queries.append(_q(out_id, "OutputTokens", dims, period))
        # Cache metrics live in the custom namespace (Project+Model dims).
        queries.append(_q(cr_id, "CacheReadInputTokens", dims, period, namespace=CACHE_NAMESPACE))
        queries.append(_q(cw_id, "CacheWriteInputTokens", dims, period, namespace=CACHE_NAMESPACE))
    # project-level totals
    proj_dims = [{"Name": "Project", "Value": project_id}]
    queries.append(_q("tin", "TotalInputTokens", proj_dims, period))
    queries.append(_q("tout", "TotalOutputTokens", proj_dims, period))

    values = {}
    for chunk in _chunks(queries, 500):
        resp = _cloudwatch.get_metric_data(
            MetricDataQueries=chunk, StartTime=midnight, EndTime=now,
        )
        for r in resp.get("MetricDataResults", []):
            values[r["Id"]] = sum(r.get("Values", []))

    # Per-model cost + totals. Cache read/write tokens count toward the daily cost (and
    # therefore toward budget_pct / enforcement), and we track the savings prompt caching
    # produced vs paying the full input rate for those cache-read tokens.
    #   cache cost    = read/1000*cache_read_rate + write/1000*cache_write_rate
    #   cache savings = read/1000*(input_rate - cache_read_rate)   [reads billed cheaper]
    # https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
    cost = 0.0
    cache_read_tokens = 0
    cache_write_tokens = 0
    cache_savings = 0.0
    for mid_key, (kind, norm) in id_map.items():
        tokens = values.get(mid_key, 0.0)
        if tokens <= 0:
            continue
        price = _model_price(norm)
        if kind == "in":
            cost += tokens / 1000.0 * price["input_price_per_1k"]
        elif kind == "out":
            cost += tokens / 1000.0 * price["output_price_per_1k"]
        elif kind == "cache_read":
            cost += tokens / 1000.0 * price["cache_read_price_per_1k"]
            cache_read_tokens += int(tokens)
            cache_savings += tokens / 1000.0 * (
                price["input_price_per_1k"] - price["cache_read_price_per_1k"]
            )
        elif kind == "cache_write":
            cost += tokens / 1000.0 * price["cache_write_price_per_1k"]
            cache_write_tokens += int(tokens)

    total_in = values.get("tin", 0.0)
    total_out = values.get("tout", 0.0)

    # A non-positive daily_budget must NOT disable enforcement (a negative value previously
    # forced budget_pct=0 -> never exceeded). Treat <= 0 as the global default so every project
    # always has a real, enforceable budget.
    daily_budget = meta["daily_budget_usd"]
    if not daily_budget or daily_budget <= 0:
        daily_budget = GLOBAL_DEFAULT_BUDGET_USD
    budget_pct = round(cost / daily_budget * 100.0, 2)
    budget_exceeded = budget_pct >= 100.0

    _budget_table.put_item(Item={
        "project_id": project_id,
        "date": now.strftime("%Y-%m-%d"),
        "input_tokens": int(total_in),
        "output_tokens": int(total_out),
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_savings_usd": Decimal(str(round(max(cache_savings, 0.0), 6))),
        "cost_today_usd": Decimal(str(round(cost, 6))),
        "daily_budget_usd": Decimal(str(daily_budget)),
        "budget_pct": Decimal(str(budget_pct)),
        "budget_exceeded": budget_exceeded,
        "updated_at": now.isoformat(),
    })


def _models_seen_today(project_id, midnight, now):
    """Fallback: discover which models a project used today by listing Project+Model metrics."""
    seen = set()
    try:
        paginator = _cloudwatch.get_paginator("list_metrics")
        for page in paginator.paginate(
            Namespace=NAMESPACE, MetricName="InputTokens",
            Dimensions=[{"Name": "Project", "Value": project_id}],
        ):
            for m in page.get("Metrics", []):
                for d in m.get("Dimensions", []):
                    if d["Name"] == "Model":
                        seen.add(d["Value"])
    except Exception:
        logger.warning("Failed to list models for a project", exc_info=True)
    return list(seen)


# Cache-rate fallback ratios (of the model's input rate), matching gw_pricing_refresh, used
# when the Pricing row predates the cache-price columns. Directionally per AWS docs: cache
# reads are cheaper than input, cache writes are a premium.
# https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
_CACHE_READ_RATIO_FALLBACK = 0.1
_CACHE_WRITE_RATIO_FALLBACK = 1.25


def _model_price(model_id):
    try:
        resp = _pricing_table.get_item(Key={"model_id": model_id})
        item = resp.get("Item")
        if item and item.get("price_available") and "input_price_per_1k" in item:
            input_rate = float(item["input_price_per_1k"])
            # Cache rates: use stored values if the row has them, else derive from input.
            cache_read = (
                float(item["cache_read_price_per_1k"])
                if "cache_read_price_per_1k" in item
                else input_rate * _CACHE_READ_RATIO_FALLBACK
            )
            cache_write = (
                float(item["cache_write_price_per_1k"])
                if "cache_write_price_per_1k" in item
                else input_rate * _CACHE_WRITE_RATIO_FALLBACK
            )
            return {
                "input_price_per_1k": input_rate,
                "output_price_per_1k": float(item["output_price_per_1k"]),
                "cache_read_price_per_1k": cache_read,
                "cache_write_price_per_1k": cache_write,
            }
    except Exception:
        logger.warning("Pricing lookup failed for a model", exc_info=True)
    # Fail-closed on missing price: charge a conservative high rate so budget math never
    # under-counts (matches the existing chargeback system's fallback posture).
    fallback_input = 0.015
    return {
        "input_price_per_1k": fallback_input,
        "output_price_per_1k": 0.075,
        "cache_read_price_per_1k": fallback_input * _CACHE_READ_RATIO_FALLBACK,
        "cache_write_price_per_1k": fallback_input * _CACHE_WRITE_RATIO_FALLBACK,
    }


def _q(qid, metric_name, dimensions, period, namespace=NAMESPACE):
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {"Namespace": namespace, "MetricName": metric_name, "Dimensions": dimensions},
            "Period": period,
            "Stat": "Sum",
        },
        "ReturnData": True,
    }


def _day_period_seconds(midnight, now):
    secs = int((now - midnight).total_seconds()) + 120
    return ((secs // 60) + 1) * 60  # multiple of 60, covers the whole day in one bucket


def _normalize(model_id):
    if model_id and "/" in model_id:
        model_id = model_id.split("/", 1)[1]
    return model_id


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
