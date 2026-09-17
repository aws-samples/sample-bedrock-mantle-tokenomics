// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Thin client for the Tokenomics HTTP API. Every call attaches the Cognito idToken as the
// Authorization header; the API Gateway JWT authorizer validates it and the Lambda enforces
// group-based authorization (/admin/* requires the `admins` group).

import { API_URL } from "./config";
import { getIdToken } from "./auth";

async function request(method, path, body) {
  const token = await getIdToken();
  if (!token) throw new Error("Not authenticated");
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      // Cognito idToken JWT sent raw in the Authorization header. The HTTP API JWT
      // authorizer (identity source $request.header.Authorization) validates it and
      // matches its `aud` claim to the app client id (idToken has aud; accessToken
      // does not -- so only the idToken is accepted here).
      Authorization: token,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json()).error || "";
    } catch {
      /* ignore non-JSON error bodies */
    }
    throw new Error(`${res.status} ${detail}`.trim());
  }
  if (res.status === 204) return null;
  return res.json();
}

// ---- admin -----------------------------------------------------------------
export const listModels = () => request("GET", "/admin/models");
export const listUsers = () => request("GET", "/admin/users");
export const listProjects = () => request("GET", "/admin/projects");
export const createProject = (p) => request("POST", "/admin/projects", p);
export const updateProject = (id, p) =>
  request("PUT", `/admin/projects/${encodeURIComponent(id)}`, p);
export const deleteProject = (id) =>
  request("DELETE", `/admin/projects/${encodeURIComponent(id)}`);
export const assignDeveloper = (a) => request("POST", "/admin/assign", a);
export const refreshModels = () => request("POST", "/admin/refresh-models");
// Invoke the aggregator now to recompute every developer's budget from current metrics.
export const refreshBudgets = () => request("POST", "/admin/refresh-budgets");
export const getUsage = () => request("GET", "/admin/usage");
export const getEnforcement = () => request("GET", "/admin/enforcement");
export const getAlerts = () => request("GET", "/admin/alerts");
// Athena-backed historical analytics. days in {7,30,90}; dim in
// {trend, cost_center, group, model, developer}.
export const getAnalytics = (days = 30, dim = "trend") =>
  request("GET", `/admin/analytics?days=${encodeURIComponent(days)}&dim=${encodeURIComponent(dim)}`);

// ---- developer (self) ------------------------------------------------------
export const getMyBudget = () => request("GET", "/me/budget");
export const getMyConfig = () => request("GET", "/me/config");
