// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Tokenomics developer portal. Always live and developer-scoped (every API call is /me/*) --
// a developer signs in with their own credentials and only ever sees their own data:
//   1. My Budget - own daily budget, spend, tokens, current tier; "Refresh now" recomputes it
//   2. Chat      - real requests through the AgentCore gateway; a live session token counter
//                  shows tokens climbing per request, and the served model reflects the tier
//   3. IDE Setup - copy-my-token helper + gateway URL + an OpenAI-compatible config snippet
// There is no demo mode: the portal's whole value is showing YOUR real budget + real traffic.
import React, { useState, useEffect, useCallback } from "react";
import {
  RadialBarChart, RadialBar, PolarAngleAxis, ResponsiveContainer,
} from "recharts";
import { IS_CONFIGURED, REGION, GATEWAY_URL, VIRTUAL_MODEL } from "./config";
import * as auth from "./auth";
import * as api from "./api";

const fmtUsd = (n) => `$${Number(n || 0).toFixed(2)}`;
const fmtTok = (n) => Number(n || 0).toLocaleString();
const shortModel = (m) => (m ? m.split(".").pop().split(":")[0] : "-");

const TABS = [
  ["budget", "My Budget"],
  ["chat", "Chat"],
  ["setup", "IDE Setup"],
];

// Resolve tier + concrete model from budget % using the developer's thresholds/ladder.
// Mirrors the interceptor's resolve_model() so the UI can show the expected tier.
function resolveTier(cfg, pct) {
  const t = (cfg && cfg.thresholds) || [75, 90, 100];
  const [lo, mid, top] = t;
  if (pct >= top) return { tier: "blocked", model: null };
  if (pct >= mid) return { tier: "tier3", model: cfg && cfg.tier3_model };
  if (pct >= lo) return { tier: "tier2", model: cfg && cfg.tier2_model };
  return { tier: "tier1", model: cfg && cfg.tier1_model };
}

// ============================================================ root
export default function App() {
  const [session, setSession] = useState(null);
  const [claims, setClaims] = useState({});
  const [checking, setChecking] = useState(true);
  const [tab, setTab] = useState("budget");

  useEffect(() => {
    (async () => {
      const s = await auth.getSession();
      if (s) { setSession(s); setClaims(await auth.getClaims()); }
      setChecking(false);
    })();
  }, []);

  const onSignedIn = async () => {
    setSession(await auth.getSession());
    setClaims(await auth.getClaims());
  };
  const onSignOut = () => { auth.signOut(); setSession(null); setClaims({}); };

  if (checking) return <div className="app"><div className="login-wrap muted">Loading...</div></div>;

  return (
    <div className="app">
      <div className="topbar">
        <div className="brand">Token<span>omics</span> · Developer</div>
        <div className="spacer" />
        {session && (
          <>
            <span className="who">{claims.email || claims["cognito:username"]}</span>
            <button className="btn secondary" onClick={onSignOut}>Sign out</button>
          </>
        )}
      </div>

      {!session ? (
        <Login onSignedIn={onSignedIn} />
      ) : (
        <div className="layout">
          <nav className="sidebar">
            {TABS.map(([id, label]) => (
              <button key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{label}</button>
            ))}
          </nav>
          <main className="content">
            <View tab={tab} />
          </main>
        </div>
      )}
    </div>
  );
}

// ============================================================ login
function Login({ onSignedIn }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [needNewPw, setNeedNewPw] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setErr(""); setBusy(true);
    try {
      const r = await auth.signIn(email, password, needNewPw ? newPassword : undefined);
      if (r.newPasswordRequired) {
        setNeedNewPw(true);
        setErr("First login: set a new password to continue.");
      } else {
        await onSignedIn();
      }
    } catch (e2) {
      setErr(e2.message || "Sign-in failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <h1>Sign in</h1>
        <p className="sub">Tokenomics developer portal · {REGION}</p>
        {!IS_CONFIGURED && (
          <div className="notice warn">This portal isn't configured with a backend yet (missing VITE_ env vars).</div>
        )}
        <label className="field"><span>Email</span>
          <input type="email" value={email} autoComplete="username"
                 onChange={(e) => setEmail(e.target.value)} required /></label>
        <label className="field"><span>Password</span>
          <input type="password" value={password} autoComplete="current-password"
                 onChange={(e) => setPassword(e.target.value)} required /></label>
        {needNewPw && (
          <label className="field"><span>New password</span>
            <input type="password" value={newPassword} autoComplete="new-password"
                   onChange={(e) => setNewPassword(e.target.value)} required /></label>
        )}
        <button className="btn" type="submit" disabled={busy || !IS_CONFIGURED}
                style={{ width: "100%", marginTop: 6 }}>
          {busy ? "Signing in..." : needNewPw ? "Set password & sign in" : "Sign in"}
        </button>
        {err && <div className="err">{err}</div>}
      </form>
    </div>
  );
}

// ============================================================ view router
function View({ tab }) {
  // Load /me/budget + /me/config once and share with all tabs.
  const [budget, setBudget] = useState(null);
  const [config, setConfig] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setErr("");
    try {
      const [b, c] = await Promise.all([api.getMyBudget(), api.getMyConfig()]);
      setBudget(b); setConfig(c);
    } catch (e) { setErr(e.message || "Failed to load"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  // "Refresh now" -> trigger an aggregator recompute, then use the returned fresh budget.
  const refreshNow = useCallback(async () => {
    setRefreshing(true); setErr("");
    try {
      // Recompute budget AND re-fetch config, so an admin change to the tier ladder / models
      // shows up on Refresh without a full page reload (config is otherwise only loaded on mount).
      const [b, c] = await Promise.all([api.refreshMine(), api.getMyConfig()]);
      if (b && !b.error) setBudget(b);
      if (c && !c.error) setConfig(c);
    } catch (e) { setErr(e.message || "Refresh failed"); }
    finally { setRefreshing(false); }
  }, []);

  if (loading) return <div className="muted">Loading...</div>;
  if (err) return <div className="notice warn">Error: {err}</div>;

  if (budget && budget.assigned === false) {
    return (
      <>
        <h1>Not assigned yet</h1>
        <div className="notice info">
          Your account isn't assigned to a project. Ask an admin to assign you in the admin console,
          then reload.
        </div>
      </>
    );
  }

  switch (tab) {
    case "budget": return <BudgetView budget={budget} config={config} onRefresh={refreshNow} refreshing={refreshing} />;
    case "chat": return <ChatView budget={budget} config={config} onRefresh={refreshNow} refreshing={refreshing} />;
    case "setup": return <SetupView />;
    default: return null;
  }
}

// ============================================================ 1. My Budget
function BudgetView({ budget, config, onRefresh, refreshing }) {
  const b = budget || {};
  const c = config || {};
  const pct = Number(b.budget_pct || 0);
  const { tier, model } = resolveTier(c, pct);
  const gaugeColor = pct >= 100 ? "#ef4444" : pct >= 90 ? "#f97316" : pct >= 75 ? "#f59e0b" : "#10b981";
  const gauge = [{ name: "budget", value: Math.min(pct, 100), fill: gaugeColor }];

  return (
    <>
      <h1>My Budget</h1>
      <p className="sub">
        Project <b>{b.project_id}</b> · {c.group} · {c.cost_center}
      </p>
      <div className="row end" style={{ marginBottom: 8, gap: 8 }}>
        <button className="btn secondary" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh now"}
        </button>
      </div>
      <p className="muted" style={{ marginTop: -2, fontSize: 12, textAlign: "right" }}>
        Budget is recomputed every ~5 min from metering; "Refresh now" forces an immediate recompute.
      </p>

      <div className="grid-2">
        <div className="panel" style={{ textAlign: "center" }}>
          <h2>Daily budget used</h2>
          <ResponsiveContainer width="100%" height={220}>
            <RadialBarChart innerRadius="70%" outerRadius="100%" data={gauge}
                            startAngle={90} endAngle={-270}>
              <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
              <RadialBar background dataKey="value" cornerRadius={12} />
            </RadialBarChart>
          </ResponsiveContainer>
          <div style={{ marginTop: -150, fontSize: 34, fontWeight: 700 }}>{pct.toFixed(0)}%</div>
          <div className="muted" style={{ marginTop: 120 }}>
            {fmtUsd(b.cost_today_usd)} of {fmtUsd(b.daily_budget_usd)} today
          </div>
        </div>

        <div>
          <div className="cards" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <Metric label="Spend today" value={fmtUsd(b.cost_today_usd)} />
            <Metric label="Daily budget" value={fmtUsd(b.daily_budget_usd)} />
            <Metric label="Input tokens" value={fmtTok(b.input_tokens)} small />
            <Metric label="Output tokens" value={fmtTok(b.output_tokens)} small />
            <Metric label="Cache read tokens" value={fmtTok(b.cache_read_tokens)} small />
            <Metric label="Cache write tokens" value={fmtTok(b.cache_write_tokens)} small />
          </div>
          <div className="panel" style={{ marginTop: 12 }}>
            <h2>Prompt-cache savings</h2>
            <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
              <span className="muted" style={{ fontSize: 13 }}>Saved today vs full input rate</span>
              <span style={{ fontSize: 24, fontWeight: 700, color: "#10b981" }}>{fmtUsd(b.cache_savings_usd)}</span>
            </div>
            <p className="muted mt" style={{ marginBottom: 0, fontSize: 12 }}>
              Cached prompt tokens are billed at a reduced rate. Cache reads still count toward your
              daily budget, but at the cheaper cache-read price — so reusing context stretches your budget.
            </p>
          </div>
          <div className="panel">
            <h2>Current tier</h2>
            {b.budget_exceeded || tier === "blocked" ? (
              <div className="notice danger" style={{ marginBottom: 0 }}>
                <b>Blocked.</b> You've reached 100% of today's budget. New requests are denied by the
                gateway policy until the budget resets or an admin raises it.
              </div>
            ) : (
              <>
                <div className="row">
                  <span className={`badge ${tier}`}>{tier}</span>
                  <span>Your <code>{VIRTUAL_MODEL}</code> resolves to <b>{shortModel(model)}</b></span>
                </div>
                <p className="muted mt" style={{ marginBottom: 0, fontSize: 13 }}>
                  As your project's daily spend rises past {(c.thresholds || [75, 90, 100]).join("% / ")}%,
                  the gateway automatically routes your requests to a more cost-efficient model.
                </p>
              </>
            )}
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>Your tier ladder</h2>
        <table>
          <thead><tr><th>Tier</th><th>Applies when budget is</th><th>Model</th><th>Active now</th></tr></thead>
          <tbody>
            <TierRow name="tier1" label={`< ${(c.thresholds || [75])[0]}%`} model={c.tier1_model} active={tier === "tier1"} />
            <TierRow name="tier2" label={`${(c.thresholds || [0,75])[0]}%–${(c.thresholds || [0,90])[1]}%`} model={c.tier2_model} active={tier === "tier2"} />
            <TierRow name="tier3" label={`${(c.thresholds || [0,90])[1]}%–${(c.thresholds || [0,0,100])[2]}%`} model={c.tier3_model} active={tier === "tier3"} />
            <tr>
              <td><span className="badge blocked">blocked</span></td>
              <td>≥ {(c.thresholds || [0,0,100])[2]}%</td>
              <td className="muted">request denied (403)</td>
              <td>{tier === "blocked" ? <span className="badge blocked">yes</span> : ""}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}

function TierRow({ name, label, model, active }) {
  return (
    <tr>
      <td><span className={`badge ${name}`}>{name}</span></td>
      <td>{label}</td>
      <td>{shortModel(model)}</td>
      <td>{active ? <span className="badge ok">yes</span> : ""}</td>
    </tr>
  );
}

// ============================================================ 2. Chat (real gateway)
// Every message is a real request through the AgentCore gateway (OpenAI-compatible). The developer
// always sends the virtual model; the gateway interceptor resolves the concrete tier model and
// Cedar enforces the budget (a real 403 when over). Each reply is stamped with the model that
// actually served it plus its token usage. A live Session-tokens counter sums the real usage as
// you send, so tokens visibly climb immediately -- the daily budget gauge catches up on the next
// aggregator recompute (or via "Refresh now").
function ChatView({ budget, config, onRefresh, refreshing }) {
  const c = config || {};
  const pct = Number((budget && budget.budget_pct) || 0);
  const [messages, setMessages] = useState([
    { role: "system", text: "You are a helpful coding assistant. Always respond in English. Live coding session. Each message is a real request through the Tokenomics gateway — the model you're served depends on your current budget tier, and requests are blocked at 100%." },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // Live session token counters (real usage summed across this session).
  const [sess, setSess] = useState({ in: 0, out: 0, total: 0, requests: 0 });

  // Concrete model + tier the gateway will serve at the current budget %, derived from the
  // developer's ladder. `model` also drives the client's route selection in send().
  const { tier, model } = resolveTier(c, pct);
  const blocked = tier === "blocked" || (budget && budget.budget_exceeded);
  const gaugeColor = pct >= 100 ? "#ef4444" : pct >= 90 ? "#f97316" : pct >= 75 ? "#f59e0b" : "#10b981";

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    const history = messages
      .filter((m) => m.role === "user" || m.role === "assistant")
      .map((m) => ({ role: m.role, content: m.text }));
    const outgoing = [...history, { role: "user", content: text }];
    setMessages((m) => [...m, { role: "user", text }]);
    setBusy(true);
    try {
      // Pass the concrete model the gateway will serve (from the tier ladder) so the client
      // picks the correct route/shape (OpenAI vs Anthropic). The gateway still resolves +
      // enforces the real model; `resolvedModel` only selects the request protocol.
      const r = await api.chatCompletion(outgoing, { resolvedModel: model });
      if (r.ok) {
        // api.chatCompletion normalizes both providers into { content, model, usage }.
        const content = r.data.content || "(empty response)";
        const usage = r.data.usage || {};
        setMessages((m) => [...m, {
          role: "assistant", text: content, model: r.data.model, usage,
        }]);
        // Accumulate real tokens into the live session counter (normalized fields).
        setSess((s) => ({
          in: s.in + (usage.prompt_tokens || 0),
          out: s.out + (usage.completion_tokens || 0),
          total: s.total + (usage.total_tokens || 0),
          requests: s.requests + 1,
        }));
      } else if (r.status === 403) {
        setMessages((m) => [...m, {
          role: "blocked",
          text: `403 Forbidden — the gateway denied this request. ${r.error}`,
        }]);
      } else {
        setMessages((m) => [...m, {
          role: "blocked",
          text: `Request failed (${r.status || "network"}): ${r.error}`,
        }]);
      }
    } finally {
      setBusy(false);
    }
  };

  const clear = () => { setMessages((m) => m.slice(0, 1)); setSess({ in: 0, out: 0, total: 0, requests: 0 }); };

  return (
    <>
      <h1>Chat</h1>
      <p className="sub">Real requests routed through the Tokenomics gateway. Watch your session tokens grow with each message; your daily budget catches up on the next recompute.</p>

      {/* live session token counters */}
      <div className="cards" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
        <Metric label="Session tokens" value={fmtTok(sess.total)} />
        <Metric label="Input" value={fmtTok(sess.in)} small />
        <Metric label="Output" value={fmtTok(sess.out)} small />
        <Metric label="Requests" value={sess.requests} small />
      </div>

      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
          <div className="row">
            {blocked
              ? <span className="badge blocked">blocked</span>
              : <><span className={`badge ${tier}`}>{tier}</span>
                  <span className="muted">expected model <b>{shortModel(model)}</b></span></>}
          </div>
          <div className="row" style={{ gap: 8 }}>
            <span className="muted">{pct.toFixed(0)}% of budget</span>
            <button className="btn secondary" onClick={onRefresh} disabled={refreshing}>
              {refreshing ? "…" : "Refresh budget"}
            </button>
            <button className="btn secondary" onClick={clear}>Clear</button>
          </div>
        </div>
        <div className="bar lg"><div style={{ width: `${Math.min(pct, 100)}%`, background: gaugeColor }} /></div>

        <div className="chat mt">
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              <div>{m.text}</div>
              {m.role === "assistant" && m.model && (
                <div className="meta">
                  {shortModel(m.model)}
                  {m.usage && m.usage.total_tokens ? ` · ${fmtTok(m.usage.total_tokens)} tok` : ""}
                </div>
              )}
            </div>
          ))}
          {busy && <div className="msg assistant"><div className="muted">Thinking…</div></div>}
        </div>

        <div className="chat-input">
          <input
            value={input}
            disabled={busy}
            placeholder="Ask the coding assistant..."
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
          />
          <button className="btn" onClick={send} disabled={busy}>{busy ? "…" : "Send"}</button>
        </div>
      </div>

      <div className="notice info">
        These are real completions. The model shown on each reply is the concrete model the gateway
        routed to based on your budget tier; a 403 here is a real Cedar policy block. Session tokens
        update instantly; the daily budget % is recomputed every ~5 min (use "Refresh budget" to
        force it).
      </div>
    </>
  );
}

// ============================================================ 3. IDE Setup
function SetupView() {
  const [token, setToken] = useState("");
  const [claims, setClaims] = useState({});
  const [copied, setCopied] = useState("");

  useEffect(() => {
    (async () => {
      // The gateway (and the IDE) authenticate with the ACCESS token, not the idToken.
      setToken((await auth.getAccessToken()) || "");
      setClaims(await auth.getClaims());
    })();
  }, []);

  const copy = async (what, value) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(what);
      setTimeout(() => setCopied(""), 1500);
    } catch {
      setCopied("(clipboard blocked — select and copy manually)");
    }
  };

  // The OpenAI-compatible base URL is the gateway's `/inference/v1` path.
  const gwRoot = (GATEWAY_URL || `https://<gateway-id>.gateway.bedrock-agentcore.${REGION}.amazonaws.com/inference`).replace(/\/+$/, "");
  const gwBase = /\/inference$/.test(gwRoot) ? gwRoot : `${gwRoot}/inference`;
  const openaiBaseUrl = `${gwBase}/v1`;
  const snippet = [
    "# OpenAI-compatible settings for your coding IDE (Cursor, Continue, Claude Code, etc.)",
    "# Base URL (Tokenomics gateway, OpenAI-compatible /inference/v1 path):",
    `OPENAI_BASE_URL=${openaiBaseUrl}`,
    "# Model: always the virtual model — the gateway resolves the real one by your budget tier",
    `OPENAI_MODEL=${VIRTUAL_MODEL}`,
    '# API key: paste your access token (from "Copy my token" above). Refresh when it expires.',
    "OPENAI_API_KEY=<your-token>",
  ].join("\n");

  return (
    <>
      <h1>IDE Setup</h1>
      <p className="sub">Point your coding IDE at the Tokenomics gateway. You always request the virtual model; the gateway resolves the concrete model and enforces your budget.</p>

      <div className="panel">
        <h2>Connection details</h2>
        <div className="kv">
          <div className="k">OpenAI base URL</div>
          <div className="v">{openaiBaseUrl}</div>
          <div className="k">Virtual model</div>
          <div className="v">{VIRTUAL_MODEL}</div>
          <div className="k">Region</div>
          <div className="v">{REGION}</div>
          <div className="k">Signed in as</div>
          <div className="v">{claims.email || "-"}</div>
        </div>
        <div className="row mt">
          <button className="btn secondary" onClick={() => copy("url", openaiBaseUrl)}>Copy base URL</button>
        </div>
      </div>

      <div className="panel">
        <h2>Copy my token</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
          Your Cognito access token authenticates your IDE to the gateway. Treat it like a password;
          it expires and can be re-copied here after signing in again.
        </p>
        <div className="token-box">{token || "(no token — sign in again)"}</div>
        <div className="row mt">
          <button className="btn" onClick={() => copy("token", token)} disabled={!token}>Copy my token</button>
          {copied && <span className="muted">Copied {copied}!</span>}
        </div>
      </div>

      <div className="panel">
        <h2>Config snippet</h2>
        <pre className="snippet">{snippet}</pre>
        <div className="row mt">
          <button className="btn secondary" onClick={() => copy("snippet", snippet)}>Copy snippet</button>
        </div>
      </div>
    </>
  );
}

// ============================================================ shared
function Metric({ label, value, small }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`value${small ? " small" : ""}`}>{value}</div>
    </div>
  );
}
