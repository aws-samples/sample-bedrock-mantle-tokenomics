# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Scheduled model + pricing refresh for Tokenomics.

Keeps the Pricing table current so the admin console can offer the models the gateway can
actually route to, and the aggregator can compute per-model cost. Runs on a daily EventBridge
schedule and can also be invoked on demand ("refresh models" in the admin console).

Design (source of truth = the bedrock-mantle model catalog):
  1. CATALOG comes from the mantle `GET /v1/models` endpoint (the OpenAI-compatible routable
     model list, e.g. `openai.gpt-5.5`, `anthropic.claude-opus-4-6`, `google.gemma-3-27b-it`).
     This is what the gateway can actually serve, so it is what the admin should pick from --
     NOT `bedrock:ListFoundationModels` (different id space) and NOT the price list alone
     (which omits the newest premium models like GPT-5 / Claude).
  2. PRICES are best-effort joined from the AWS Price List API. The price list has no model-id
     attribute; the id is embedded in `usagetype` (e.g. "USE2-deepseek.v3.2-input-tokens"), so
     we match a routable id's model segment against the usagetype string. Unit is "1K tokens",
     so pricePerUnit.USD is the per-1K price.
  3. Every routable model is written to the Pricing table with `price_available` true/false and
     the ROUTABLE id as `model_id`. Models the price list hasn't published yet (GPT-5, Claude)
     are still selectable; the aggregator fail-closes their cost to a conservative rate so
     budget math stays safe.

There is no boto3/CLI client for bedrock-mantle, so the model list is fetched via a
SigV4-signed HTTPS request to the Projects/OpenAI-compatible REST endpoint.
"""
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PRICING_TABLE = os.environ["PRICING_TABLE"]
# The region whose models + prices we resolve (where models are actually invoked).
TARGET_REGION = os.environ.get("AWS_REGION", "us-east-2")

_dynamodb = boto3.resource("dynamodb")
_pricing_table = _dynamodb.Table(PRICING_TABLE)
# Price List API is only available in us-east-1 / ap-south-1.
_pricing = boto3.client("pricing", region_name="us-east-1")
_session = boto3.session.Session()

_MANTLE_HOST = f"bedrock-mantle.{TARGET_REGION}.api.aws"

# Variant qualifiers we skip so we price the STANDARD on-demand token cost, not
# cache/video/batch/priority/flex rows. Open-weight models served via Bedrock "mantle" publish
# their token price under a "-standard" (on-demand) usagetype alongside cheaper "-flex" and
# "-batch"/"-priority" variants; we must keep ONLY the "-standard" row as authoritative so a
# cheaper flex/batch rate is never stored and under-enforces budgets.
_SKIP_VARIANTS = ("cache", "video", "batch", "priority", "image", "-audio", "flex")

# Prompt-cache token unit prices are separate CUR/price-list line items with these usagetype
# substrings (verified against the AWS Bedrock Cost & Usage Report token-type table):
#   cache read  -> "*-cache-read-input-token-count"   (cheaper than input)
#   cache write -> "*-cache-write-input-token-count"  (more expensive than input)
# https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-understanding-cur-data.html
_CACHE_READ_MARKER = "cache-read-input-token-count"
_CACHE_WRITE_MARKER = "cache-write-input-token-count"

# Fallback ratios (of the model's standard input rate) when a model has no published cache
# price row yet. Directionally correct per AWS docs: reads are billed at a reduced rate,
# writes at a premium. https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
_CACHE_READ_RATIO_FALLBACK = 0.1
_CACHE_WRITE_RATIO_FALLBACK = 1.25


def lambda_handler(event, context):
    catalog = _list_routable_models()
    if not catalog:
        logger.warning("Mantle returned no routable models; leaving Pricing table unchanged")
        return {"statusCode": 200, "body": json.dumps({"models": 0, "priced": 0})}

    price_rows = _fetch_price_rows(TARGET_REGION)
    now = datetime.now(timezone.utc).isoformat()

    priced = 0
    for model_id in catalog:
        prices = _match_price(model_id, price_rows)
        item = {"model_id": model_id, "last_updated": now}
        if prices:
            input_price = prices["input"]
            item["input_price_per_1k"] = Decimal(str(input_price))
            item["output_price_per_1k"] = Decimal(str(prices["output"]))
            item["price_available"] = True
            priced += 1
            # Cache rates: use published price-list rows when available, otherwise derive from
            # the standard input rate with the doc-directional fallback ratios.
            cache_read = prices.get("cache_read")
            cache_write = prices.get("cache_write")
            item["cache_price_available"] = bool(
                cache_read is not None and cache_write is not None
            )
            if cache_read is None:
                cache_read = input_price * _CACHE_READ_RATIO_FALLBACK
            if cache_write is None:
                cache_write = input_price * _CACHE_WRITE_RATIO_FALLBACK
            item["cache_read_price_per_1k"] = Decimal(str(round(cache_read, 8)))
            item["cache_write_price_per_1k"] = Decimal(str(round(cache_write, 8)))
        else:
            # Routable but no published on-demand price yet (e.g. newest GPT-5 / Claude).
            # Still selectable; the aggregator fail-closes cost to a conservative rate.
            item["price_available"] = False
            item["cache_price_available"] = False
        _pricing_table.put_item(Item=item)

    logger.info("Refresh complete: %d routable models, %d with published price", len(catalog), priced)
    return {"statusCode": 200, "body": json.dumps({"models": len(catalog), "priced": priced})}


# --------------------------------------------------------------------------- mantle catalog
def _list_routable_models():
    """Return the list of routable model ids from the mantle `GET /v1/models` endpoint.

    This is the authoritative catalog of what the gateway can serve. Signed with SigV4; the
    host is a hardcoded template (no user input in the URL), and we hard-validate scheme+host
    before opening the connection (defense-in-depth)."""
    path = "/v1/models"
    url = f"https://{_MANTLE_HOST}{path}"
    creds = _session.get_credentials().get_frozen_credentials()
    req = AWSRequest(method="GET", url=url, headers={"Host": _MANTLE_HOST})
    SigV4Auth(creds, "bedrock-mantle", TARGET_REGION).add_auth(req)
    prepared = req.prepare()
    parsed = urlparse(prepared.url)
    if parsed.scheme != "https" or parsed.hostname != _MANTLE_HOST:
        raise ValueError("mantle models URL failed scheme/host validation")
    r = urllib.request.Request(prepared.url, method="GET")  # nosec B310 # nosemgrep: dynamic-urllib-use-detected - HTTPS+host validated; hardcoded host template
    for k, v in prepared.headers.items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:  # nosec B310 # nosemgrep: dynamic-urllib-use-detected - HTTPS+host validated; hardcoded host template
            data = json.load(resp)
    except Exception:
        logger.warning("Failed to list routable models from mantle", exc_info=True)
        return []
    items = data.get("data", data if isinstance(data, list) else [])
    ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
    return sorted(set(ids))


# --------------------------------------------------------------------------- price list
def _direction(low_usagetype):
    # Cache rows contain "input-token" too, so classify them FIRST and exclude them from the
    # standard input/output direction (they are priced on their own cache_* fields).
    if _CACHE_READ_MARKER in low_usagetype:
        return "cache_read"
    if _CACHE_WRITE_MARKER in low_usagetype:
        return "cache_write"
    if "input-token" in low_usagetype:
        return "input"
    if "output-token" in low_usagetype:
        return "output"
    return None


def _first_ondemand_price(product):
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            if usd is not None:
                try:
                    return float(usd)
                except (TypeError, ValueError):
                    return None
    return None


def _fetch_price_rows(target_region):
    """Return a list of (usagetype_lower, direction, price_per_1k) for standard on-demand token
    rows in the region. Used to join prices onto the routable model catalog."""
    rows = []
    params = {
        "ServiceCode": "AmazonBedrock",
        "Filters": [{"Type": "TERM_MATCH", "Field": "regionCode", "Value": target_region}],
    }
    next_token = None
    try:
        while True:
            if next_token:
                params["NextToken"] = next_token
            resp = _pricing.get_products(**params)
            for raw in resp.get("PriceList", []):
                product = json.loads(raw) if isinstance(raw, str) else raw
                usagetype = product.get("product", {}).get("attributes", {}).get("usagetype", "")
                low = usagetype.lower()
                if "token" not in low:
                    continue
                # Classify direction first (cache read/write are recognized ahead of the
                # generic input/output). Then drop only the OTHER non-standard variants
                # (video/batch/priority/image/audio) -- but KEEP cache rows, which we price.
                direction = _direction(low)
                if not direction:
                    continue
                is_cache = direction in ("cache_read", "cache_write")
                if not is_cache and any(v in low for v in _SKIP_VARIANTS):
                    continue
                price = _first_ondemand_price(product)
                if price is not None:
                    rows.append((low, direction, price))
            next_token = resp.get("NextToken")
            if not next_token:
                break
    except Exception:
        logger.warning("Failed to fetch AmazonBedrock price list", exc_info=True)
    return rows


def _match_price(model_id, price_rows):
    """Join a routable model id to a price by matching its model segment against usagetypes.

    e.g. "openai.gpt-oss-120b" -> core "gpt-oss-120b" -> matches
         "use2-openai.gpt-oss-120b-input-tokens". Returns a dict with "input"/"output" (and,
         when published, "cache_read"/"cache_write") or None if the standard input/output pair
         is missing (can't compute cost from a half price)."""
    mid = model_id.lower()
    parts = mid.split(".", 1)
    core = parts[1] if len(parts) > 1 else mid
    # Looser variants: drop a trailing -YYYY-MM-DD date suffix some ids carry. Compute it for
    # BOTH the full id (provider.model) and the provider-stripped core, since the usagetype
    # segment may retain the provider prefix.
    _date = r"-\d{4}-\d{2}-\d{2}$"
    loose = re.sub(_date, "", core)
    mid_loose = re.sub(_date, "", mid)

    # ANCHORED match: the usagetype embeds the model id between a region prefix and a direction
    # suffix, e.g. "use2-openai.gpt-oss-120b-input-tokens". We require the model id (or its
    # provider-stripped core / date-stripped loose form) to appear as a WHOLE token bounded by
    # `-`, `.`, or string ends -- NOT an arbitrary substring. This prevents a shorter id (e.g.
    # "deepseek.v3") from cross-matching a sibling's row ("deepseek.v3.2"), which previously let
    # a cheaper sibling's rate be stored and UNDER-enforce budgets. We also do NOT take a blind
    # min() across different models; a boundary match ties a usagetype to exactly one model, and
    # min() now only breaks ties among rows for that SAME model (e.g. regional duplicates).
    def _seg(ut):
        # The model segment sits between the region prefix (first '-') and the direction suffix.
        # Strip a leading "region-" prefix if present, then peel the direction/variant suffixes
        # so the residue is exactly the model id.
        #
        # Two published shapes are handled:
        #   1. Standard   -> "use2-openai.gpt-oss-120b-input-tokens"
        #   2. Mantle     -> "deepseek.v3.1-mantle-input-tokens-standard"
        # Open-weight models served via Bedrock "mantle" carry a "-mantle-" infix before the
        # direction and an on-demand "-standard" variant suffix after "-tokens". We drop the
        # trailing "-standard" first, then the "-<direction>-tokens" (allowing the optional
        # "-mantle" infix), so both shapes reduce to the bare model id ("deepseek.v3.1").
        s = ut
        s = re.sub(r"^[a-z0-9]+-", "", s, count=1)          # drop region prefix (e.g. "use2-")
        s = re.sub(r"-standard$", "", s)                    # drop on-demand variant suffix (mantle)
        s = re.sub(r"(?:-mantle)?-(input|output|cache[- ]?read|cache[- ]?write)-tokens$", "", s)
        s = re.sub(r"(?:-mantle)?-tokens$", "", s)
        return s

    _wanted = {mid, core, loose, mid_loose}

    def _matches(ut):
        return _seg(ut) in _wanted

    def cheapest(direction):
        cand = [pr for (ut, d, pr) in price_rows if d == direction and _matches(ut)]
        return min(cand) if cand else None

    pin, pout = cheapest("input"), cheapest("output")
    if pin is None or pout is None:
        return None
    result = {"input": pin, "output": pout}
    # Cache prices are optional (many models have no published cache row); include them only
    # when present. The caller fills missing cache rates with the ratio fallback.
    cread, cwrite = cheapest("cache_read"), cheapest("cache_write")
    if cread is not None:
        result["cache_read"] = cread
    if cwrite is not None:
        result["cache_write"] = cwrite
    return result
