// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Static demo data so the console renders (and demos) without a live backend.
// Shape matches the API responses exactly, so views are identical in Live and Demo mode.

export const DEMO_MODELS = {
  models: [
    { model_id: "anthropic.claude-3-5-haiku-20241022-v1:0", input_price_per_1k: 0.0008, output_price_per_1k: 0.004 },
    { model_id: "anthropic.claude-3-5-sonnet-20241022-v2:0", input_price_per_1k: 0.003, output_price_per_1k: 0.015 },
    { model_id: "anthropic.claude-3-7-sonnet-20250219-v1:0", input_price_per_1k: 0.003, output_price_per_1k: 0.015 },
    { model_id: "anthropic.claude-opus-4-20250514-v1:0", input_price_per_1k: 0.015, output_price_per_1k: 0.075 },
    { model_id: "amazon.nova-lite-v1:0", input_price_per_1k: 0.00006, output_price_per_1k: 0.00024 },
    { model_id: "amazon.nova-pro-v1:0", input_price_per_1k: 0.0008, output_price_per_1k: 0.0032 },
  ],
};

export const DEMO_USERS = {
  users: [
    { sub: "sub-alice", username: "alice", email: "alice@example.com", status: "CONFIRMED" },
    { sub: "sub-bob", username: "bob", email: "bob@example.com", status: "CONFIRMED" },
    { sub: "sub-carol", username: "carol", email: "carol@example.com", status: "CONFIRMED" },
    { sub: "sub-dave", username: "dave", email: "dave@example.com", status: "FORCE_CHANGE_PASSWORD" },
  ],
};

export const DEMO_PROJECTS = {
  projects: [
    {
      project_id: "proj_platform_x1", name: "Platform Core", group: "platform", cost_center: "CC-1001",
      daily_budget_usd: 25, allowed_models: ["amazon.nova-pro-v1:0", "anthropic.claude-3-5-sonnet-20241022-v2:0", "anthropic.claude-opus-4-20250514-v1:0"],
      tier1_model: "anthropic.claude-opus-4-20250514-v1:0",
      tier2_model: "anthropic.claude-3-5-sonnet-20241022-v2:0",
      tier3_model: "amazon.nova-pro-v1:0",
      thresholds: [75, 90, 100],
      developers: [{ sub: "sub-alice", email: "alice@example.com" }, { sub: "sub-bob", email: "bob@example.com" }],
    },
    {
      project_id: "proj_data_y2", name: "Data Science", group: "data", cost_center: "CC-2002",
      daily_budget_usd: 10, allowed_models: ["amazon.nova-lite-v1:0", "anthropic.claude-3-5-haiku-20241022-v1:0", "anthropic.claude-3-5-sonnet-20241022-v2:0"],
      tier1_model: "anthropic.claude-3-5-sonnet-20241022-v2:0",
      tier2_model: "anthropic.claude-3-5-haiku-20241022-v1:0",
      tier3_model: "amazon.nova-lite-v1:0",
      thresholds: [70, 85, 100],
      developers: [{ sub: "sub-carol", email: "carol@example.com" }],
    },
  ],
};

export const DEMO_USAGE = {
  total_cost_today_usd: 21.34,
  total_cache_savings_usd: 6.18,
  // Customer-project rollups (sum of each project's developers' per-developer spend).
  projects: [
    { customer_project: "cproj_platform", name: "Platform Engineering", group: "platform", cost_center: "CC-1001", developers: 2, cost_today_usd: 18.92, input_tokens: 2450000, output_tokens: 512000, cache_read_tokens: 1830000, cache_write_tokens: 240000, cache_savings_usd: 5.41, budget_exceeded: 1 },
    { customer_project: "cproj_data", name: "Data Science", group: "data-science", cost_center: "CC-2002", developers: 1, cost_today_usd: 2.42, input_tokens: 680000, output_tokens: 141000, cache_read_tokens: 295000, cache_write_tokens: 38000, cache_savings_usd: 0.77, budget_exceeded: 0 },
  ],
  // Per-developer detail (each has their own mantle project).
  developers: [
    { developer: "alice@example.com", customer_project: "cproj_platform", customer_project_name: "Platform Engineering", mantle_project_id: "proj_alice7x", group: "platform", cost_center: "CC-1001", cost_today_usd: 14.10, budget_pct: 94.0, budget_exceeded: false, input_tokens: 1820000, output_tokens: 380000, cache_read_tokens: 1360000, cache_write_tokens: 178000, cache_savings_usd: 4.02 },
    { developer: "bob@example.com", customer_project: "cproj_platform", customer_project_name: "Platform Engineering", mantle_project_id: "proj_bob4k", group: "platform", cost_center: "CC-1001", cost_today_usd: 4.82, budget_pct: 32.1, budget_exceeded: false, input_tokens: 630000, output_tokens: 132000, cache_read_tokens: 470000, cache_write_tokens: 62000, cache_savings_usd: 1.39 },
    { developer: "carol@example.com", customer_project: "cproj_data", customer_project_name: "Data Science", mantle_project_id: "proj_carol9m", group: "data-science", cost_center: "CC-2002", cost_today_usd: 2.42, budget_pct: 24.2, budget_exceeded: false, input_tokens: 680000, output_tokens: 141000, cache_read_tokens: 295000, cache_write_tokens: 38000, cache_savings_usd: 0.77 },
  ],
  by_group: [
    { group: "platform", cost_today_usd: 18.92 },
    { group: "data-science", cost_today_usd: 2.42 },
  ],
  by_cost_center: [
    { cost_center: "CC-1001", cost_today_usd: 18.92 },
    { cost_center: "CC-2002", cost_today_usd: 2.42 },
  ],
};

export const DEMO_ENFORCEMENT = {
  enforcement: [
    { developer: "alice@example.com", customer_project: "cproj_platform", customer_project_name: "Platform Engineering", mantle_project_id: "proj_alice7x", group: "platform", cost_center: "CC-1001", budget_pct: 94.0, state: "tier3", resolved_model: "google.gemma-3-4b-it", blocked: false },
    { developer: "bob@example.com", customer_project: "cproj_platform", customer_project_name: "Platform Engineering", mantle_project_id: "proj_bob4k", group: "platform", cost_center: "CC-1001", budget_pct: 32.1, state: "tier1", resolved_model: "openai.gpt-5.5", blocked: false },
    { developer: "carol@example.com", customer_project: "cproj_data", customer_project_name: "Data Science", mantle_project_id: "proj_carol9m", group: "data-science", cost_center: "CC-2002", budget_pct: 24.2, state: "tier1", resolved_model: "anthropic.claude-opus-4-6", blocked: false },
  ],
};

export const DEMO_ALERTS = {
  alerts: [
    { developer: "alice@example.com", customer_project: "cproj_platform", customer_project_name: "Platform Engineering", group: "platform", cost_center: "CC-1001", budget_pct: 94.0, cost_today_usd: 14.10, type: "WARNING", updated_at: "2026-08-29T14:05:00Z" },
  ],
};

// 24h trend used by the usage view (demo only; live view derives from current snapshot).
export const DEMO_TREND = Array.from({ length: 12 }, (_, i) => {
  const hour = i * 2;
  return {
    time: `${String(hour).padStart(2, "0")}:00`,
    platform: Number((0.4 + i * 0.16 + Math.sin(i) * 0.2).toFixed(2)),
    data: Number((0.05 + i * 0.02).toFixed(2)),
  };
});

// Historical analytics (demo fallback for the Athena-backed Analytics view + Dashboard trend).
// Row shapes mirror GET /admin/analytics responses exactly:
//   trend:       [{ day, input_tokens, output_tokens, cost_usd }]
//   cost_center: [{ key, input_tokens, output_tokens, cost_usd }]
//   group:       [{ key, input_tokens, output_tokens, cost_usd }]
//   developer:   [{ key, input_tokens, output_tokens, cost_usd }]
//   model:       [{ model, input_tokens, output_tokens, cost_usd, priced }]
function _demoTrend(days) {
  const out = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    // weekday-weighted wave with mild growth so the trend reads like real adoption
    const dow = d.getDay();
    const weekday = dow === 0 || dow === 6 ? 0.45 : 1;
    const growth = 1 + (days - i) / (days * 2.2);
    const wave = 0.8 + 0.25 * Math.sin(i / 3);
    const inTok = Math.round(220000 * weekday * growth * wave);
    const outTok = Math.round(inTok * 0.21);
    const cacheRead = Math.round(inTok * 0.7);   // heavy prompt reuse
    const cacheWrite = Math.round(inTok * 0.12);
    const cost = inTok / 1000 * 0.015 + outTok / 1000 * 0.075
      + cacheRead / 1000 * 0.0015 + cacheWrite / 1000 * 0.01875;
    const cacheSaved = cacheRead / 1000 * (0.015 - 0.0015);
    out.push({
      day: d.toISOString().slice(0, 10),
      input_tokens: inTok,
      output_tokens: outTok,
      cache_read_tokens: cacheRead,
      cache_write_tokens: cacheWrite,
      cache_savings_usd: Number(cacheSaved.toFixed(4)),
      cost_usd: Number(cost.toFixed(4)),
    });
  }
  return out;
}

export const DEMO_ANALYTICS = {
  trend: (days = 30) => ({ dim: "trend", days, data: _demoTrend(days) }),
  cost_center: {
    dim: "cost_center", days: 30,
    data: [
      { key: "CC-1001", input_tokens: 5120000, output_tokens: 1075000, cache_read_tokens: 3580000, cache_write_tokens: 610000, cache_savings_usd: 48.33, cost_usd: Number((5120 * 0.015 + 1075 * 0.075 + 3580 * 0.0015 + 610 * 0.01875).toFixed(4)) },
      { key: "CC-2002", input_tokens: 2360000, output_tokens: 495000, cache_read_tokens: 1650000, cache_write_tokens: 280000, cache_savings_usd: 22.28, cost_usd: Number((2360 * 0.015 + 495 * 0.075 + 1650 * 0.0015 + 280 * 0.01875).toFixed(4)) },
      { key: "CC-3003", input_tokens: 1180000, output_tokens: 248000, cache_read_tokens: 820000, cache_write_tokens: 140000, cache_savings_usd: 11.07, cost_usd: Number((1180 * 0.015 + 248 * 0.075 + 820 * 0.0015 + 140 * 0.01875).toFixed(4)) },
    ],
  },
  group: {
    dim: "group", days: 30,
    data: [
      { key: "platform", input_tokens: 5120000, output_tokens: 1075000, cache_read_tokens: 3580000, cache_write_tokens: 610000, cache_savings_usd: 48.33, cost_usd: Number((5120 * 0.015 + 1075 * 0.075 + 3580 * 0.0015 + 610 * 0.01875).toFixed(4)) },
      { key: "data-science", input_tokens: 2360000, output_tokens: 495000, cache_read_tokens: 1650000, cache_write_tokens: 280000, cache_savings_usd: 22.28, cost_usd: Number((2360 * 0.015 + 495 * 0.075 + 1650 * 0.0015 + 280 * 0.01875).toFixed(4)) },
      { key: "mobile", input_tokens: 1180000, output_tokens: 248000, cache_read_tokens: 820000, cache_write_tokens: 140000, cache_savings_usd: 11.07, cost_usd: Number((1180 * 0.015 + 248 * 0.075 + 820 * 0.0015 + 140 * 0.01875).toFixed(4)) },
    ],
  },
  developer: {
    dim: "developer", days: 30,
    data: [
      { key: "alice@example.com", input_tokens: 3400000, output_tokens: 714000, cache_read_tokens: 2380000, cache_write_tokens: 400000, cache_savings_usd: 32.13, cost_usd: Number((3400 * 0.015 + 714 * 0.075 + 2380 * 0.0015 + 400 * 0.01875).toFixed(4)) },
      { key: "carol@example.com", input_tokens: 2360000, output_tokens: 495000, cache_read_tokens: 1650000, cache_write_tokens: 280000, cache_savings_usd: 22.28, cost_usd: Number((2360 * 0.015 + 495 * 0.075 + 1650 * 0.0015 + 280 * 0.01875).toFixed(4)) },
      { key: "bob@example.com", input_tokens: 1720000, output_tokens: 361000, cache_read_tokens: 1200000, cache_write_tokens: 205000, cache_savings_usd: 16.20, cost_usd: Number((1720 * 0.015 + 361 * 0.075 + 1200 * 0.0015 + 205 * 0.01875).toFixed(4)) },
      { key: "dev@tokenomics.demo", input_tokens: 1180000, output_tokens: 248000, cache_read_tokens: 820000, cache_write_tokens: 140000, cache_savings_usd: 11.07, cost_usd: Number((1180 * 0.015 + 248 * 0.075 + 820 * 0.0015 + 140 * 0.01875).toFixed(4)) },
    ],
  },
  model: {
    dim: "model", days: 30,
    data: [
      { model: "openai.gpt-5.5", input_tokens: 2600000, output_tokens: 546000, cache_read_tokens: 1820000, cache_write_tokens: 310000, cache_savings_usd: 24.57, cost_usd: 89.4, priced: true },
      { model: "anthropic.claude-opus-4-6", input_tokens: 1800000, output_tokens: 378000, cache_read_tokens: 1260000, cache_write_tokens: 215000, cache_savings_usd: 17.01, cost_usd: 61.2, priced: true },
      { model: "anthropic.claude-haiku-4-5", input_tokens: 1500000, output_tokens: 315000, cache_read_tokens: 1050000, cache_write_tokens: 180000, cache_savings_usd: 14.18, cost_usd: 12.8, priced: true },
      { model: "deepseek.v3.2", input_tokens: 1200000, output_tokens: 252000, cache_read_tokens: 840000, cache_write_tokens: 140000, cache_savings_usd: 11.34, cost_usd: 6.4, priced: true },
      { model: "google.gemma-3-4b-it", input_tokens: 980000, output_tokens: 205000, cache_read_tokens: 686000, cache_write_tokens: 115000, cache_savings_usd: 9.26, cost_usd: 1.9, priced: true },
      { model: "mistral.mistral-large-3-675b-instruct", input_tokens: 660000, output_tokens: 140000, cache_read_tokens: 0, cache_write_tokens: 0, cache_savings_usd: 0, cost_usd: 3.1, priced: false },
    ],
  },
};
