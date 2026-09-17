// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Thin client for the Tokenomics HTTP API, scoped to the developer's own data (/me/*).
// Attaches the Cognito idToken as the Authorization header; the API Gateway JWT authorizer
// validates it and the Lambda scopes every /me/* response to the caller's own `sub`.

import { API_URL, GATEWAY_URL, VIRTUAL_MODEL } from "./config";
import { getIdToken, getAccessToken } from "./auth";

async function request(method, path) {
  const token = await getIdToken();
  if (!token) throw new Error("Not authenticated");
  const res = await fetch(`${API_URL}${path}`, {
    method,
    // Cognito idToken JWT sent raw; the HTTP API JWT authorizer matches its `aud` claim
    // to the app client id (idToken has aud; accessToken does not).
    headers: { Authorization: token },
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
  return res.json();
}

// The developer only ever reads their own budget + config.
export const getMyBudget = () => request("GET", "/me/budget");
export const getMyConfig = () => request("GET", "/me/config");
// Trigger an on-demand budget recompute (aggregator sweep) and get the caller's fresh budget back.
export const refreshMine = () => request("POST", "/me/refresh");

// ---------------------------------------------------------------------------
// Real inference through the AgentCore gateway — TRI-PROTOCOL.
//
// The gateway exposes THREE inference routes, each serving a different model family:
//   * /v1/chat/completions  — Chat Completions (OpenAI-compatible): open-weight families
//     (deepseek.*, qwen.*, mistral.*, google.gemma*, meta.*, nvidia.*, zai.*) and
//     openai.gpt-oss-* (open-weight GPT).
//   * /v1/messages          — Anthropic Messages: anthropic.claude-* (requires the
//     anthropic-version header).
//   * /v1/responses         — OpenAI Responses API: proprietary openai.gpt-5.*, openai.gpt-4*,
//     openai.o* models. Request uses `input` (message array), response has `output_text`.
// Sending a model on the wrong route yields HTTP 400 "isn't supported on this route".
//
// The portal always requests the VIRTUAL model alias, but it ALSO knows (client-side, from the
// budget tier ladder) which CONCRETE model will be served. We use that concrete model only to
// pick the correct ROUTE + request/response shape; the gateway interceptor still resolves and
// enforces the real model + budget. We authenticate with the ACCESS token.
const _ANTHROPIC_VERSION = "2023-06-01";

// Route detection from a concrete model id. Prefix-based because mantle model metadata does not
// expose a route field. Verified live against the deployed gateway.
const _RESPONSES_API_PREFIXES = ["openai.gpt-5", "openai.gpt-4", "openai.o"];

export function isAnthropicModel(modelId) {
  return typeof modelId === "string" && /^anthropic\./i.test(modelId);
}

export function isResponsesApiModel(modelId) {
  return typeof modelId === "string" &&
    _RESPONSES_API_PREFIXES.some((p) => modelId.startsWith(p));
}

function _inferenceBase() {
  const base = (GATEWAY_URL || "").replace(/\/+$/, "");
  if (!base) throw new Error("Gateway URL not configured");
  return /\/inference$/.test(base) ? base : `${base}/inference`;
}

// Split an OpenAI-style messages array into an Anthropic {system, messages} pair.
function _toAnthropic(messages) {
  const system = messages
    .filter((m) => m.role === "system")
    .map((m) => m.content)
    .join("\n\n");
  const msgs = messages
    .filter((m) => m.role === "user" || m.role === "assistant")
    .map((m) => ({ role: m.role, content: m.content }));
  return { system: system || undefined, messages: msgs };
}

// Normalize any provider's usage object into one shape the UI can rely on.
function _normalizeUsage(usage) {
  usage = usage || {};
  if ("input_tokens" in usage || "output_tokens" in usage) {
    // Anthropic shape (+ prompt-cache token lines when present).
    const inTok = usage.input_tokens || 0;
    const outTok = usage.output_tokens || 0;
    const cacheRead = usage.cache_read_input_tokens || 0;
    const cacheWrite = usage.cache_creation_input_tokens || 0;
    return {
      prompt_tokens: inTok,
      completion_tokens: outTok,
      total_tokens: inTok + outTok,
      cache_read_tokens: cacheRead,
      cache_write_tokens: cacheWrite,
    };
  }
  // OpenAI shape. Chat Completions uses prompt_tokens/completion_tokens with cache reads under
  // prompt_tokens_details.cached_tokens. The Responses API uses input_tokens/output_tokens with
  // cache reads+writes under input_tokens_details (implicit caching for GPT-5.x, explicit for
  // GPT-5.6). Normalize both.
  const itd = usage.input_tokens_details || {};
  const ptd = usage.prompt_tokens_details || {};
  const promptTok = usage.prompt_tokens || usage.input_tokens || 0;
  const completionTok = usage.completion_tokens || usage.output_tokens || 0;
  return {
    prompt_tokens: promptTok,
    completion_tokens: completionTok,
    total_tokens: usage.total_tokens || (promptTok + completionTok),
    cache_read_tokens: ptd.cached_tokens || itd.cached_tokens || 0,
    cache_write_tokens: itd.cache_write_tokens || usage.cache_write_tokens || 0,
  };
}

/**
 * Send a chat through the gateway, auto-selecting Chat Completions, Anthropic Messages, or the
 * OpenAI Responses API based on the concrete model that will be served.
 * @param {Array<{role:string,content:string}>} messages OpenAI-style messages (role+content).
 * @param {object} [opts] { model, resolvedModel, maxTokens }.
 *   - model: the model to REQUEST (defaults to the virtual alias).
 *   - resolvedModel: the concrete model the gateway will serve (from the tier ladder). Used
 *     ONLY to pick the route/shape. Falls back to `model` when omitted.
 * @returns {Promise<{ok:true, data:{content,model,usage,raw}} | {ok:false, status, error}>}
 *   The success `data` is normalized across all three providers so the UI is provider-agnostic.
 *   Never throws on a 403 budget block — returns {ok:false,...} so the UI can render it.
 */
export async function chatCompletion(messages, opts = {}) {
  const token = await getAccessToken();
  if (!token) throw new Error("Not authenticated");

  const requestModel = opts.model || VIRTUAL_MODEL;
  const routeModel = opts.resolvedModel || requestModel;
  const maxTokens = opts.maxTokens || 512;

  // Pick route + request shape by the concrete model family.
  const anthropic = isAnthropicModel(routeModel);
  const responses = !anthropic && isResponsesApiModel(routeModel);

  let url, headers, body;
  headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };

  if (anthropic) {
    // Anthropic Messages route: /v1/messages
    url = `${_inferenceBase()}/v1/messages`;
    headers["anthropic-version"] = _ANTHROPIC_VERSION;
    const { system, messages: amsgs } = _toAnthropic(messages);
    body = { model: requestModel, messages: amsgs, max_tokens: maxTokens };
    if (system) body.system = system;
  } else if (responses) {
    // OpenAI Responses API: /v1/responses — uses `input` (message array), not `messages`.
    // `store: false` avoids server-side state retention for ephemeral portal chat.
    url = `${_inferenceBase()}/v1/responses`;
    const input = messages
      .filter((m) => m.role === "user" || m.role === "assistant" || m.role === "system" || m.role === "developer")
      .map((m) => ({ role: m.role === "system" ? "developer" : m.role, content: m.content }));
    body = { model: requestModel, input, max_output_tokens: maxTokens, store: false };
  } else {
    // Chat Completions: /v1/chat/completions (default for open-weight + gpt-oss).
    url = `${_inferenceBase()}/v1/chat/completions`;
    body = { model: requestModel, messages, max_tokens: maxTokens };
  }

  let res;
  try {
    res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
  } catch (e) {
    return { ok: false, status: 0, error: e.message || "Network error" };
  }
  let payload = null;
  try { payload = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const msg =
      (payload && payload.error && (payload.error.message || payload.error)) ||
      (payload && payload.message) ||
      `HTTP ${res.status}`;
    return { ok: false, status: res.status, error: String(msg) };
  }

  // Normalize the success payload across all three providers.
  let content, servedModel, usage;
  if (anthropic) {
    // Anthropic: { content: [{type:"text", text}], model, usage:{input_tokens,...} }
    const blocks = Array.isArray(payload.content) ? payload.content : [];
    content = blocks.filter((b) => b && b.type === "text").map((b) => b.text).join("") ||
      "(empty response)";
    servedModel = payload.model;
    usage = _normalizeUsage(payload.usage);
  } else if (responses) {
    // Responses API: { output_text, model, usage:{prompt_tokens,...}, output:[{content:[{text}]}] }
    content = payload.output_text ||
      (Array.isArray(payload.output) && payload.output
        .flatMap((o) => (o.content || []).filter((c) => c.type === "output_text").map((c) => c.text))
        .join("")) ||
      "(empty response)";
    servedModel = payload.model;
    usage = _normalizeUsage(payload.usage);
  } else {
    // Chat Completions: { choices:[{message:{content}}], model, usage:{prompt_tokens,...} }
    const choice = (payload.choices && payload.choices[0]) || {};
    content = (choice.message && choice.message.content) || "(empty response)";
    servedModel = payload.model;
    usage = _normalizeUsage(payload.usage);
  }
  return { ok: true, data: { content, model: servedModel, usage, raw: payload } };
}
