# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tokenomics Firehose transform (analytics enrichment).

Firehose delivers CloudWatch Metric Stream records (AWS/BedrockMantle namespace) as
newline-delimited JSON. Each metric record carries dimensions Project and (for per-model
metrics) Model, plus a value stat set {min,max,sum,count} and a timestamp -- but NO
CostCenter/Group/Developer tags (mantle metric dimensions are fixed to Project + Model).

This transform enriches each record with CostCenter / Group / Developer looked up from the
project registry so S3 + Athena can GROUP BY CostCenter, Group, Model for near-real-time
per-model / per-cost-center token reporting.

Contract (Firehose data transformation):
  - Input:  event["records"][*] = {recordId, data (base64), ...}
  - Output: {"records": [{recordId, result: "Ok"|"Dropped"|"ProcessingFailed", data (base64)}]}
  - Every input record MUST be returned or Firehose drops it.

Project -> tags lookup: the interceptor keys on the developer (sub), but the metric records
carry project_id. We resolve project_id -> tags via the config table's `by-project` GSI and
cache within the invocation to avoid repeated lookups for the same project.
"""
import base64
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONFIG_TABLE = os.environ["CONFIG_TABLE"]
PROJECT_INDEX = os.environ.get("PROJECT_INDEX", "by-project")

_dynamodb = boto3.resource("dynamodb")
_config_table = _dynamodb.Table(CONFIG_TABLE)


def _project_tags(project_id: str, cache: dict) -> dict:
    """Resolve {CostCenter, Group, Developer} for a project_id (invocation-cached)."""
    if project_id in cache:
        return cache[project_id]
    tags = {}
    try:
        resp = _config_table.query(
            IndexName=PROJECT_INDEX,
            KeyConditionExpression=boto3.dynamodb.conditions.Key("project_id").eq(project_id),
            Limit=1,
        )
        items = resp.get("Items", [])
        if items:
            item = items[0]
            tags = {
                "CostCenter": item.get("cost_center", "unknown"),
                "Group": item.get("group", "unknown"),
                "Developer": item.get("developer_email") or item.get("sub", "unknown"),
            }
    except Exception:
        logger.warning("Tag lookup failed for a project; leaving record un-enriched")
    cache[project_id] = tags
    return tags


def lambda_handler(event, context):
    out_records = []
    cache = {}
    for rec in event.get("records", []):
        rid = rec["recordId"]
        try:
            raw = base64.b64decode(rec["data"]).decode("utf-8")
            lines = [ln for ln in raw.split("\n") if ln.strip()]
            enriched = []
            for line in lines:
                obj = json.loads(line)
                project_id = (obj.get("dimensions") or {}).get("Project")
                if project_id:
                    tags = _project_tags(project_id, cache)
                    if tags:
                        obj.update(tags)
                enriched.append(json.dumps(obj))
            new_data = ("\n".join(enriched) + "\n").encode("utf-8")
            out_records.append({
                "recordId": rid,
                "result": "Ok",
                "data": base64.b64encode(new_data).decode("utf-8"),
            })
        except Exception:
            logger.warning("Failed to process a Firehose record; marking ProcessingFailed")
            out_records.append({"recordId": rid, "result": "ProcessingFailed", "data": rec["data"]})
    return {"records": out_records}
