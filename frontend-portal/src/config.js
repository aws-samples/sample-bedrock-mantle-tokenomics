// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Runtime configuration — resolved at PAGE LOAD, not baked into the bundle.
//
// Precedence:
//   1. window.__ENV__  — injected by /env.js, which CDK writes into the hosting bucket from
//      the deployed stack's own outputs. This makes one build portable to ANY account/region.
//   2. import.meta.env.VITE_*  — build-time Vite vars, used only for local `npm run dev`.
//   3. "" — empty when unconfigured (login/live mode stays disabled; demo mode still works).
//
// There are intentionally NO hardcoded account/region/URL/model defaults here: every real
// value comes from the stack at deploy time, so the frontend is fully self-contained.

const _env = (typeof window !== "undefined" && window.__ENV__) || {};

function _val(runtimeKey, viteVal) {
  const v = _env[runtimeKey];
  if (v !== undefined && v !== null && v !== "") return v;
  return viteVal || "";
}

export const API_URL = _val("API_URL", import.meta.env.VITE_API_URL).replace(/\/+$/, "");
export const USER_POOL_ID = _val("USER_POOL_ID", import.meta.env.VITE_USER_POOL_ID);
export const USER_POOL_CLIENT_ID = _val("USER_POOL_CLIENT_ID", import.meta.env.VITE_USER_POOL_CLIENT_ID);
export const REGION = _val("REGION", import.meta.env.VITE_REGION);
export const GATEWAY_URL = _val("GATEWAY_URL", import.meta.env.VITE_GATEWAY_URL);
export const VIRTUAL_MODEL = _val("VIRTUAL_MODEL", import.meta.env.VITE_VIRTUAL_MODEL);

// True only when every value needed for a live login + API call is present.
export const IS_CONFIGURED = Boolean(
  API_URL && USER_POOL_ID && USER_POOL_CLIENT_ID
);
