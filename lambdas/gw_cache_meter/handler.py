# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tokenomics cache meter (AgentCore Gateway RESPONSE interceptor, HTTP/inference target).

Runs AFTER the gateway has the full (buffered) model response and BEFORE the gateway replies
to the caller. Its only job is to read the prompt-cache token counts from the response `usage`
block and emit them as CloudWatch metrics so the aggregator can price them into the developer's
daily budget and the frontends can show cache usage + savings.

Why a RESPONSE interceptor + a correlation table (not just read the request):
  For HTTP/inference targets the RESPONSE interceptor input has `gatewayRequest: null` -- it
  receives ONLY the response (statusCode/headers/body base64). It therefore cannot see which
  developer/project the request belonged to. Both interceptor invocations DO receive the same
  `REQUEST_ID` in the Lambda client context, so the REQUEST interceptor stamps
  `REQUEST_ID -> project_id + model` into a short-TTL DynamoDB table and this Lambda reads it
  back to attribute the cache tokens per project.
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html

Cache token shapes handled (from the response `usage` object):
  Anthropic (Messages API):
    usage.cache_read_input_tokens        -> cache READ  (served from cache; cheap)
    usage.cache_creation_input_tokens    -> cache WRITE (written to cache; premium)
    usage.cache_creation.ephemeral_5m_input_tokens / ephemeral_1h_input_tokens (detail; the
      top-level cache_creation_input_tokens is the sum, so we use it directly)
  OpenAI (Chat Completions):
    usage.prompt_tokens_details.cached_tokens -> cache READ only (OpenAI has no cache-write line)

Design constraints:
  - Buffered (non-streaming) responses only. HTTP-target response interceptors are not invoked
    in streaming mode, and if `isStreamingResponse` is ever set we skip (documented limitation).
  - Never transform the response. Always return a passthrough so a metering bug can never
    corrupt or drop a model response: {"interceptorOutputVersion": "1.0", "http": {}}.
  - Metric emission and mapping lookups are best-effort; any failure is swallowed after logging.
"""
import base64
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REQMAP_TABLE = os.environ.get("REQMAP_TABLE")
CACHE_NAMESPACE = os.environ.get("CACHE_NAMESPACE", "Tokenomics/Cache")
TARGET_PREFIX = os.environ.get("TARGET_PREFIX", "bedrock-models")

_dynamodb = boto3.resource("dynamodb")
_reqmap_table = _dynamodb.Table(REQMAP_TABLE) if REQMAP_TABLE else None
_cloudwatch = boto3.client("cloudwatch")

# Passthrough output: leave the response exactly as the target produced it.
_PASSTHROUGH = {"interceptorOutputVersion": "1.0", "http": {}}


# ---------------------------------------------------------------------------
# Client context / correlation
# ---------------------------------------------------------------------------
def _request_id(context) -> str:
    """Read REQUEST_ID from the Lambda client context (always present for HTTP targets)."""
    try:
        cc = getattr(context, "client_context", None)
        custom = getattr(cc, "custom", None) or {}
        return custom.get("REQUEST_ID") or ""
    except Exception:
        return ""


def _lookup_mapping(request_id: str) -> dict:
    """REQUEST_ID -> {project_id, model} written by the request interceptor. Empty if absent."""
    if not (_reqmap_table and request_id):
        return {}
    try:
        item = _reqmap_table.get_item(Key={"request_id": request_id}).get("Item") or {}
        return {"project_id": item.get("project_id"), "model": item.get("model")}
    except Exception:
        logger.warning("ReqMap lookup failed for a request")
        return {}


def _normalize(model: str) -> str:
    if model and "/" in model:
        return model.split("/", 1)[1]
    return model or ""


# ---------------------------------------------------------------------------
# usage parsing
# ---------------------------------------------------------------------------
def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_cache_tokens(body: dict) -> tuple[int, int]:
    """Return (cache_read_tokens, cache_write_tokens) from a parsed response body.

    Handles both the Anthropic and OpenAI usage shapes. Returns (0, 0) when there is no usage
    block or no cache activity."""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0, 0

    # Anthropic Messages API.
    read = _to_int(usage.get("cache_read_input_tokens"))
    write = _to_int(usage.get("cache_creation_input_tokens"))
    # If the top-level write count is absent but the ephemeral breakdown is present, sum it.
    if write == 0:
        cc = usage.get("cache_creation")
        if isinstance(cc, dict):
            write = _to_int(cc.get("ephemeral_5m_input_tokens")) + \
                _to_int(cc.get("ephemeral_1h_input_tokens"))

    # OpenAI Chat Completions (read-only cache; no cache-write line).
    if read == 0:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            read = _to_int(details.get("cached_tokens"))

    # OpenAI Responses API (bedrock-mantle): cache tokens live under input_tokens_details, and
    # this shape DOES report a cache-write count (unlike Chat Completions). Implicit caching for
    # GPT-5.x and explicit caching for GPT-5.6 both surface here.
    #   usage.input_tokens_details.cached_tokens   -> cache READ
    #   usage.input_tokens_details.cache_write_tokens (or usage.cache_write_tokens) -> cache WRITE
    itd = usage.get("input_tokens_details")
    if isinstance(itd, dict):
        if read == 0:
            read = _to_int(itd.get("cached_tokens"))
        if write == 0:
            write = _to_int(itd.get("cache_write_tokens"))
    if write == 0:
        write = _to_int(usage.get("cache_write_tokens"))

    return read, write


def _model_from_body(body: dict) -> str:
    """Model id echoed in the response body (fallback when the mapping is missing)."""
    return _normalize(body.get("model") or "")


# ---------------------------------------------------------------------------
# metric emission
# ---------------------------------------------------------------------------
def _emit(project_id: str, model: str, cache_read: int, cache_write: int) -> None:
    """PutMetricData to the custom Tokenomics/Cache namespace, dims Project+Model.

    Emits only the metrics that are > 0 to avoid zero-noise. Uses a custom namespace (NOT the
    AWS-reserved AWS/BedrockMantle) because PutMetricData rejects AWS/* namespaces."""
    dims = [{"Name": "Project", "Value": project_id}, {"Name": "Model", "Value": model or "unknown"}]
    data = []
    if cache_read > 0:
        data.append({"MetricName": "CacheReadInputTokens", "Dimensions": dims,
                     "Value": float(cache_read), "Unit": "Count"})
    if cache_write > 0:
        data.append({"MetricName": "CacheWriteInputTokens", "Dimensions": dims,
                     "Value": float(cache_write), "Unit": "Count"})
    if not data:
        return
    try:
        _cloudwatch.put_metric_data(Namespace=CACHE_NAMESPACE, MetricData=data)
        logger.info("Cache metrics emitted project=%s model=%s read=%d write=%d",
                    project_id, model, cache_read, cache_write)
    except Exception:
        logger.warning("Failed to put cache metrics (metering only; response unaffected)")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event, context):
    # Never let metering affect the response: any failure -> clean passthrough.
    try:
        http = event.get("http", {}) or {}
        resp = http.get("gatewayResponse", {}) or {}

        # Streaming responses are not metered (HTTP-target response interceptors are buffered
        # only; guard anyway in case streaming lands later).
        if resp.get("isStreamingResponse"):
            return _PASSTHROUGH

        body_b64 = resp.get("body")
        if not body_b64:
            # Body excluded by a payload filter, or an empty/streamed body -> nothing to meter.
            return _PASSTHROUGH

        try:
            body = json.loads(base64.b64decode(body_b64))
        except (ValueError, TypeError):
            return _PASSTHROUGH
        if not isinstance(body, dict):
            return _PASSTHROUGH

        cache_read, cache_write = _extract_cache_tokens(body)
        if cache_read <= 0 and cache_write <= 0:
            return _PASSTHROUGH  # no cache activity; skip the correlation lookup entirely

        mapping = _lookup_mapping(_request_id(context))
        project_id = mapping.get("project_id")
        model = mapping.get("model") or _model_from_body(body)
        if not project_id:
            # Can't attribute without a project. Log and skip (do not emit a bogus dimension).
            logger.info("Cache tokens present but no project mapping (read=%d write=%d)",
                        cache_read, cache_write)
            return _PASSTHROUGH

        _emit(project_id, model, cache_read, cache_write)
    except Exception:
        logger.warning("Cache meter error (ignored; response passthrough)", exc_info=True)
    return _PASSTHROUGH
