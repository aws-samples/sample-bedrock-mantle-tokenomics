// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Tokenomics admin console. Grouped-sidebar layout:
//   OVERVIEW   - Dashboard (summary cards, budget-health gauge, spend trend, cost-by-model, top spenders)
//   ANALYTICS  - Usage (per-customer-project rollup + per-developer detail)
//   GOVERNANCE - Enforcement (live per-developer tier/block), Alerts (near/over budget, with badge)
//   CONFIGURE  - Projects (budget/tier ladder/thresholds), Models (priced catalog), Developers (assign)
//   SYSTEM     - Architecture (tabbed), API Explorer (live Try It), Cost Analysis (scenario)
//   + Live/Demo toggle (bundled DEMO_DATA when no live backend / for presenting)
import React, { useState, useEffect, useCallback } from "react";
import {
  BarChart, Bar, AreaChart, Area, LineChart, Line, PieChart, Pie, Cell,
  RadialBarChart, RadialBar, PolarAngleAxis,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import { IS_CONFIGURED, REGION, API_URL } from "./config";
import * as auth from "./auth";
import * as api from "./api";
import {
  DEMO_MODELS, DEMO_USERS, DEMO_PROJECTS, DEMO_USAGE,
  DEMO_ENFORCEMENT, DEMO_ALERTS, DEMO_ANALYTICS,
} from "./demoData";

const CHART_COLORS = ["#0ea5a4", "#7c5cff", "#f59e0b", "#38bdf8", "#fb7185", "#6366f1"];
const fmtUsd = (n) => `$${Number(n || 0).toFixed(2)}`;
const fmtTok = (n) => Number(n || 0).toLocaleString();
const shortModel = (m) => (m ? m.split(".").pop().split(":")[0] : "-");

// ---- sidebar structure: grouped sections -> nav items -----------------------
const NAV_ITEMS = {
  dashboard:   { label: "Dashboard",     icon: "◈" },
  usage:       { label: "Usage",         icon: "▤" },
  analytics:   { label: "Analytics",     icon: "▲" },
  enforcement: { label: "Enforcement",   icon: "⚖" },
  alerts:      { label: "Alerts",        icon: "⚠" },
  projects:    { label: "Projects",      icon: "▦" },
  models:      { label: "Models",        icon: "◆" },
  developers:  { label: "Developers",    icon: "☺" },
  architecture:{ label: "Architecture",  icon: "⚙" },
  api:         { label: "API Explorer",  icon: "◍" },
  cost:        { label: "Cost Analysis", icon: "◉" },
  simulation:  { label: "Simulation",    icon: "▶" },
};
const NAV_SECTIONS = [
  { title: "OVERVIEW",   items: ["dashboard"] },
  { title: "ANALYTICS",  items: ["usage", "analytics"] },
  { title: "GOVERNANCE", items: ["enforcement", "alerts"] },
  { title: "CONFIGURE",  items: ["projects", "models", "developers"] },
  { title: "SYSTEM",     items: ["architecture", "api", "cost"] },
  { title: "DEMO",       items: ["simulation"] },
];

// ============================================================ root
export default function App() {
  const [session, setSession] = useState(null);
  const [claims, setClaims] = useState({});
  const [admin, setAdmin] = useState(false);
  const [checking, setChecking] = useState(true);
  const [mode, setMode] = useState(IS_CONFIGURED ? "live" : "demo"); // live | demo
  const [tab, setTab] = useState("dashboard");
  const [alertCount, setAlertCount] = useState(0);

  useEffect(() => {
    (async () => {
      const s = await auth.getSession();
      if (s) {
        setSession(s);
        setClaims(await auth.getClaims());
        setAdmin(await auth.isAdmin());
      }
      setChecking(false);
    })();
  }, []);

  const demo = mode === "demo";
  const authed = demo || Boolean(session);

  // Keep the Alerts nav badge in sync with current alert count.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (demo) { setAlertCount((DEMO_ALERTS.alerts || []).length); return; }
      if (!session) { setAlertCount(0); return; }
      try {
        const r = await api.getAlerts();
        if (!cancelled) setAlertCount(((r && r.alerts) || []).length);
      } catch { if (!cancelled) setAlertCount(0); }
    })();
    return () => { cancelled = true; };
  }, [demo, session]);

  const onSignedIn = async () => {
    setSession(await auth.getSession());
    setClaims(await auth.getClaims());
    setAdmin(await auth.isAdmin());
  };
  const onSignOut = () => {
    auth.signOut();
    setSession(null); setClaims({}); setAdmin(false);
  };

  if (checking) {
    return <div className="app-loading">Loading…</div>;
  }

  if (!authed) {
    return <Login onSignedIn={onSignedIn} />;
  }

  return (
    <div className="app-layout">
      <Sidebar
        tab={tab} setTab={setTab} mode={mode} setMode={setMode}
        alertCount={alertCount}
        who={claims.email || claims["cognito:username"]}
        admin={admin} session={session} onSignOut={onSignOut}
      />
      <main className="main">
        <header className="page-header">
          <div>
            <h1 className="page-title">{NAV_ITEMS[tab].label}</h1>
            <div className="page-crumb">Tokenomics · Admin · {REGION}</div>
          </div>
          {session && (
            <div className="page-user">
              <span className="who">{claims.email || claims["cognito:username"]}</span>
              <span className={`role-pill ${admin ? "admin" : "dev"}`}>{admin ? "admin" : "developer"}</span>
            </div>
          )}
        </header>
        <div className="page-body">
          {mode === "live" && !admin && session && (
            <div className="notice warn">
              You are signed in as a developer (not in the <b>admins</b> group). Admin API calls
              return 403 — switch to <b>Demo</b> to preview the full console.
            </div>
          )}
          <View tab={tab} demo={demo} />
        </div>
      </main>
    </div>
  );
}

function Sidebar({ tab, setTab, mode, setMode, alertCount, who, admin, session, onSignOut }) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div className="sidebar-logo">Token<span>omics</span></div>
        <div className="sidebar-tag">Cost Governance</div>
      </div>

      <div className="sidebar-mode" title={IS_CONFIGURED ? "" : "Live requires VITE_ env config"}>
        <button className={mode === "live" ? "active" : ""} disabled={!IS_CONFIGURED}
                onClick={() => setMode("live")}>Live</button>
        <button className={mode === "demo" ? "active" : ""}
                onClick={() => setMode("demo")}>Demo</button>
      </div>

      <nav className="sidebar-nav">
        {NAV_SECTIONS.map((section) => (
          <div key={section.title} className="nav-section">
            <div className="nav-section-title">{section.title}</div>
            {section.items.map((id) => {
              const item = NAV_ITEMS[id];
              return (
                <button key={id} className={`nav-item ${tab === id ? "active" : ""}`}
                        onClick={() => setTab(id)}>
                  <span className="nav-icon">{item.icon}</span>
                  <span className="nav-label">{item.label}</span>
                  {id === "alerts" && alertCount > 0 && (
                    <span className="nav-badge">{alertCount}</span>
                  )}
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        {session ? (
          <>
            <div className="sidebar-user">
              <div className="sidebar-user-name">{who}</div>
              <div className="sidebar-user-role">{admin ? "admin" : "developer"}</div>
            </div>
            <button className="btn secondary block" onClick={onSignOut}>Sign out</button>
          </>
        ) : (
          <div className="sidebar-user-role">Demo mode</div>
        )}
      </div>
    </aside>
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
        <div className="login-logo">Token<span>omics</span></div>
        <p className="sub">Admin console · {REGION}</p>
        {!IS_CONFIGURED && (
          <div className="notice warn">
            No live config detected. Set VITE_ env vars to enable sign-in, or use Demo mode.
          </div>
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
        <button className="btn block" type="submit" disabled={busy || !IS_CONFIGURED}>
          {busy ? "Signing in…" : needNewPw ? "Set password & sign in" : "Sign in"}
        </button>
        {err && <div className="err">{err}</div>}
      </form>
    </div>
  );
}

// ============================================================ view router
function View({ tab, demo }) {
  switch (tab) {
    case "dashboard": return <DashboardView demo={demo} />;
    case "usage": return <UsageView demo={demo} />;
    case "analytics": return <AnalyticsView demo={demo} />;
    case "projects": return <ProjectsView demo={demo} />;
    case "developers": return <DevelopersView demo={demo} />;
    case "models": return <ModelsView demo={demo} />;
    case "enforcement": return <EnforcementView demo={demo} />;
    case "alerts": return <AlertsView demo={demo} />;
    case "architecture": return <ArchitectureView />;
    case "api": return <ApiExplorerView demo={demo} />;
    case "cost": return <CostAnalysisView />;
    case "simulation": return <SimulationView demo={demo} />;
    default: return null;
  }
}

// shared loader hook: pick live loader or demo payload based on mode.
function useData(demo, liveFn, demoPayload, deps = []) {
  const [data, setData] = useState(demo ? demoPayload : null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(!demo);
  const load = useCallback(async () => {
    if (demo) { setData(demoPayload); setLoading(false); return; }
    setLoading(true); setErr("");
    try { setData(await liveFn()); }
    catch (e) { setErr(e.message || "Failed to load"); }
    finally { setLoading(false); }
  }, [demo]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [load, ...deps]); // eslint-disable-line react-hooks/exhaustive-deps
  return { data, err, loading, reload: load };
}

function Status({ loading, err }) {
  if (loading) return <div className="muted">Loading…</div>;
  if (err) return <div className="notice warn">Error: {err}</div>;
  return null;
}

// ============================================================ OVERVIEW · Dashboard
function DashboardView({ demo }) {
  const { data, err, loading } = useData(demo, api.getUsage, DEMO_USAGE);
  // Real 14-day trend from Athena (Live); demo curve otherwise. This makes the trend charts
  // respect the Live/Demo toggle instead of always showing sample data.
  const trend = useData(demo, () => api.getAnalytics(7, "trend"), DEMO_ANALYTICS.trend(14));
  if (loading || err) return <Status loading={loading} err={err} />;
  const u = data || {};
  const projects = u.projects || [];
  const developers = u.developers || [];

  const totalCost = Number(u.total_cost_today_usd || 0);
  const totalIn = projects.reduce((a, p) => a + (p.input_tokens || 0), 0);
  const totalOut = projects.reduce((a, p) => a + (p.output_tokens || 0), 0);
  const totalTokens = totalIn + totalOut;
  const blockedDevs = developers.filter((d) => d.budget_exceeded).length;
  // Prompt-cache savings: dollars avoided vs paying full input rate for cache-read tokens.
  const totalCacheSavings = Number(
    u.total_cache_savings_usd != null
      ? u.total_cache_savings_usd
      : projects.reduce((a, p) => a + (p.cache_savings_usd || 0), 0)
  );
  const totalCacheRead = projects.reduce((a, p) => a + (p.cache_read_tokens || 0), 0);
  const totalCacheWrite = projects.reduce((a, p) => a + (p.cache_write_tokens || 0), 0);

  // Budget health = average of per-developer budget %, gauge-colored.
  const avgPct = developers.length
    ? developers.reduce((a, d) => a + Number(d.budget_pct || 0), 0) / developers.length
    : 0;
  const gaugeColor = avgPct >= 100 ? "var(--blocked)" : avgPct >= 90 ? "var(--tier3)" : avgPct >= 75 ? "var(--tier2)" : "var(--tier1)";
  const gauge = [{ name: "budget", value: Math.min(avgPct, 100), fill: gaugeColor }];

  // Trend series (real in Live, demo otherwise): map analytics rows to chart points.
  const trendRows = (trend.data && trend.data.data) || [];
  const trendData = trendRows.map((r) => ({
    time: (r.day || "").slice(5), // MM-DD
    spend: Number(r.cost_usd || 0),
  }));

  // Cost-by-project donut + top spenders (developers).
  const pie = projects.map((p) => ({ name: p.name, value: Number(p.cost_today_usd || 0) }));
  const topDevs = [...developers].sort((a, b) => (b.cost_today_usd || 0) - (a.cost_today_usd || 0)).slice(0, 5);
  const maxDev = topDevs[0]?.cost_today_usd || 1;

  return (
    <>
      {/* hero stat cards */}
      <div className="stat-grid">
        <div className="stat-card hero">
          <div className="stat-label">Total spend today</div>
          <div className="stat-value">{fmtUsd(totalCost)}</div>
          <div className="stat-spark">
            <ResponsiveContainer width="100%" height={40}>
              <AreaChart data={trendData}>
                <Area type="monotone" dataKey="spend" stroke="var(--accent)" fill="var(--accent-fade)" strokeWidth={2} dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total tokens</div>
          <div className="stat-value">{fmtTok(totalTokens)}</div>
          <div className="stat-sub"><span>{fmtTok(totalIn)} in</span><span>{fmtTok(totalOut)} out</span></div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Active developers</div>
          <div className="stat-value">{developers.length}</div>
          <div className="stat-sub"><span>{projects.length} projects</span>{blockedDevs > 0 && <span className="danger">{blockedDevs} blocked</span>}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Cache savings today</div>
          <div className="stat-value ok">{fmtUsd(totalCacheSavings)}</div>
          <div className="stat-sub"><span>{fmtTok(totalCacheRead)} read</span><span>{fmtTok(totalCacheWrite)} write</span></div>
        </div>
        <div className="stat-card budget">
          <div className="stat-label">Budget health</div>
          <div className="gauge">
            <ResponsiveContainer width={92} height={92}>
              <RadialBarChart cx="50%" cy="50%" innerRadius="70%" outerRadius="100%" data={gauge} startAngle={90} endAngle={-270}>
                <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
                <RadialBar background dataKey="value" cornerRadius={8} />
              </RadialBarChart>
            </ResponsiveContainer>
            <div className="gauge-label">{avgPct.toFixed(0)}%</div>
          </div>
          <div className="stat-sub"><span className="ok">avg budget used</span></div>
        </div>
      </div>

      {/* charts row */}
      <div className="grid-2">
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h2>Spend trend</h2>
            <span className="muted" style={{ fontSize: 11 }}>{demo ? "sample" : "from analytics"}</span>
          </div>
          <ResponsiveContainer width="100%" height={240}>
            <AreaChart data={trendData}>
              <defs>
                <linearGradient id="dgrad1" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="var(--accent)" stopOpacity={0.35} />
                  <stop offset="95%" stopColor="var(--accent)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
              <XAxis dataKey="time" stroke="var(--muted)" fontSize={11} axisLine={false} tickLine={false} />
              <YAxis stroke="var(--muted)" fontSize={11} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} />
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
              <Area type="monotone" dataKey="spend" name="Daily spend" stroke="var(--accent)" strokeWidth={2} fill="url(#dgrad1)" />
            </AreaChart>
          </ResponsiveContainer>
          {trendData.length === 0 && <div className="muted" style={{ fontSize: 12 }}>No historical data yet — analytics populates as developers send traffic.</div>}
        </div>
        <div className="panel">
          <h2>Spend by project</h2>
          <ResponsiveContainer width="100%" height={240}>
            <PieChart>
              <Pie data={pie} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={55} outerRadius={85} paddingAngle={3}>
                {pie.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
              </Pie>
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
            </PieChart>
          </ResponsiveContainer>
          <div className="pie-legend">
            {pie.map((d, i) => (
              <div key={i} className="pie-legend-item">
                <span className="dot" style={{ background: CHART_COLORS[i % CHART_COLORS.length] }} />
                <span className="pie-name">{d.name}</span>
                <span className="pie-val">{fmtUsd(d.value)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* top spenders */}
      <div className="panel">
        <h2>Top spenders (developers)</h2>
        <div className="spenders">
          {topDevs.map((d, i) => {
            const pct = ((d.cost_today_usd || 0) / maxDev) * 100;
            return (
              <div key={d.mantle_project_id || i} className="spender-row">
                <div className="spender-rank" style={{ background: CHART_COLORS[i % CHART_COLORS.length] }}>{i + 1}</div>
                <div className="spender-info">
                  <div className="spender-name">{d.developer}</div>
                  <div className="spender-sub">{d.customer_project_name} · {fmtTok((d.input_tokens || 0) + (d.output_tokens || 0))} tokens</div>
                </div>
                <div className="spender-bar-wrap">
                  <div className="spender-bar" style={{ width: `${pct}%`, background: CHART_COLORS[i % CHART_COLORS.length] }} />
                </div>
                <div className="spender-cost">{fmtUsd(d.cost_today_usd)}</div>
              </div>
            );
          })}
          {topDevs.length === 0 && <div className="muted">No developer usage yet.</div>}
        </div>
      </div>
    </>
  );
}

// ============================================================ ANALYTICS · Usage
function UsageView({ demo }) {
  const { data, err, loading, reload } = useData(demo, api.getUsage, DEMO_USAGE);
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState("");
  const refreshBudgets = async () => {
    if (demo) { setMsg("(demo) would invoke the aggregator to recompute budgets"); return; }
    setRefreshing(true); setMsg("");
    try {
      await api.refreshBudgets();
      setMsg("Budget recompute started. Metrics can lag 1-2 min; reload shortly to see updates.");
      setTimeout(() => { reload(); }, 6000);
    } catch (e) { setMsg(e.message); }
    finally { setRefreshing(false); }
  };
  if (loading || err) return <Status loading={loading} err={err} />;
  const u = data || {};
  const projects = u.projects || [];
  const developers = u.developers || [];
  return (
    <>
      <p className="sub">Current-day token spend, per developer and rolled up per customer project (refreshes every ~5 min from mantle metering).</p>
      <div className="row" style={{ justifyContent: "flex-end", gap: 10, marginBottom: 12 }}>
        <button className="btn secondary" onClick={reload} disabled={loading || demo}>Reload</button>
        <button className="btn" onClick={refreshBudgets} disabled={refreshing}>
          {refreshing ? "Recomputing…" : "Refresh budgets now"}
        </button>
      </div>
      {msg && <div className="notice info">{msg}</div>}
      <div className="stat-grid">
        <Metric label="Total spend today" value={fmtUsd(u.total_cost_today_usd)} />
        <Metric label="Cache savings today" value={fmtUsd(
          u.total_cache_savings_usd != null
            ? u.total_cache_savings_usd
            : projects.reduce((a, p) => a + (p.cache_savings_usd || 0), 0)
        )} />
        <Metric label="Developers" value={developers.length} />
        <Metric label="Input tokens" value={fmtTok(projects.reduce((a, p) => a + (p.input_tokens || 0), 0))} />
        <Metric label="Output tokens" value={fmtTok(projects.reduce((a, p) => a + (p.output_tokens || 0), 0))} />
        <Metric label="Cache read tokens" value={fmtTok(projects.reduce((a, p) => a + (p.cache_read_tokens || 0), 0))} />
      </div>

      <div className="grid-2">
        <div className="panel">
          <h2>Spend by group</h2>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={u.by_group || []}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
              <XAxis dataKey="group" stroke="var(--muted)" fontSize={12} axisLine={false} tickLine={false} />
              <YAxis stroke="var(--muted)" fontSize={12} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} />
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
              <Bar dataKey="cost_today_usd" name="Spend" fill="var(--accent)" radius={[6, 6, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="panel">
          <h2>Spend by cost center</h2>
          <ResponsiveContainer width="100%" height={240}>
            <PieChart>
              <Pie data={u.by_cost_center || []} dataKey="cost_today_usd" nameKey="cost_center"
                   cx="50%" cy="50%" outerRadius={85} label={(e) => e.cost_center}>
                {(u.by_cost_center || []).map((_, i) => (
                  <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                ))}
              </Pie>
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="panel">
        <h2>Spend by customer project</h2>
        <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
          Each customer project's spend is the sum of its developers' individual (per-developer
          mantle project) token cost.
        </p>
        <table>
          <thead>
            <tr><th>Customer project</th><th>Group</th><th>Cost center</th><th>Developers</th>
              <th>Spend today</th><th>In tokens</th><th>Out tokens</th>
              <th>Cache read</th><th>Cache write</th><th>Cache saved</th><th>Over budget</th></tr>
          </thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.customer_project}>
                <td><b>{p.name}</b></td><td>{p.group}</td><td>{p.cost_center}</td>
                <td>{p.developers}</td>
                <td>{fmtUsd(p.cost_today_usd)}</td>
                <td>{fmtTok(p.input_tokens)}</td><td>{fmtTok(p.output_tokens)}</td>
                <td>{fmtTok(p.cache_read_tokens)}</td><td>{fmtTok(p.cache_write_tokens)}</td>
                <td className="ok">{fmtUsd(p.cache_savings_usd)}</td>
                <td>{p.budget_exceeded > 0
                  ? <span className="badge blocked">{p.budget_exceeded}</span>
                  : <span className="badge ok">0</span>}</td>
              </tr>
            ))}
            {projects.length === 0 && <tr><td colSpan={11} className="muted">No projects yet.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Per-developer spend</h2>
        <table>
          <thead>
            <tr><th>Developer</th><th>Customer project</th><th>Mantle project (cost id)</th>
              <th>Spend</th><th>Budget %</th><th>In tokens</th><th>Out tokens</th>
              <th>Cache read</th><th>Cache write</th><th>Cache saved</th></tr>
          </thead>
          <tbody>
            {developers.map((d) => (
              <tr key={d.mantle_project_id}>
                <td>{d.developer}</td>
                <td>{d.customer_project_name}</td>
                <td className="mono muted">{d.mantle_project_id}</td>
                <td>{fmtUsd(d.cost_today_usd)}</td>
                <td style={{ width: 140 }}><BudgetBar pct={d.budget_pct} /></td>
                <td>{fmtTok(d.input_tokens)}</td><td>{fmtTok(d.output_tokens)}</td>
                <td>{fmtTok(d.cache_read_tokens)}</td><td>{fmtTok(d.cache_write_tokens)}</td>
                <td className="ok">{fmtUsd(d.cache_savings_usd)}</td>
              </tr>
            ))}
            {developers.length === 0 && <tr><td colSpan={10} className="muted">No developers assigned yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ============================================================ ANALYTICS · Analytics (Athena)
function AnalyticsView({ demo }) {
  const [range, setRange] = useState(30);
  const [sub, setSub] = useState("trend"); // trend | cost_center | group | model | developer
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  // Resolve demo payload for the current sub-tab.
  const demoPayload = () => {
    switch (sub) {
      case "trend": return DEMO_ANALYTICS.trend(range);
      case "cost_center": return DEMO_ANALYTICS.cost_center;
      case "group": return DEMO_ANALYTICS.group;
      case "model": return DEMO_ANALYTICS.model;
      case "developer": return DEMO_ANALYTICS.developer;
      default: return { data: [] };
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (demo) { setData(demoPayload()); setErr(""); setLoading(false); return; }
      setLoading(true); setErr("");
      try {
        const r = await api.getAnalytics(range, sub);
        if (!cancelled) setData(r);
      } catch (e) {
        if (!cancelled) setErr(e.message || "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [demo, range, sub]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = (data && data.data) || [];
  const SUBS = [
    ["trend", "Trends"], ["cost_center", "Cost Centers"], ["group", "Groups"],
    ["model", "Models"], ["developer", "Developers"],
  ];

  return (
    <>
      <p className="sub">Historical token spend from the analytics data lake (CloudWatch metric stream → Firehose → S3), queried live through Athena. Spend is derived from per-model pricing; token counts are exact.</p>

      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10, marginBottom: 14 }}>
        <div className="tabs">
          {SUBS.map(([id, label]) => (
            <button key={id} className={`tab ${sub === id ? "active" : ""}`} onClick={() => setSub(id)}>{label}</button>
          ))}
        </div>
        <div className="scenario-row" style={{ margin: 0 }}>
          {[[1, "Today"], [7, "7d"], [30, "30d"], [90, "90d"]].map(([d, label]) => (
            <button key={d} className={`scenario-btn ${range === d ? "active" : ""}`} onClick={() => setRange(d)}>{label}</button>
          ))}
        </div>
      </div>

      {demo && <div className="notice info">Demo mode — sample analytics. Switch to Live (admin login) to query the real Athena data lake.</div>}
      <Status loading={loading} err={err} />

      {!loading && !err && (
        sub === "trend"
          ? <AnalyticsTrend rows={rows} demo={demo} />
          : <AnalyticsBreakdown rows={rows} dim={sub} />
      )}
    </>
  );
}

function AnalyticsTrend({ rows, demo }) {
  const series = rows.map((r) => ({
    day: (r.day || "").slice(5),
    spend: Number(r.cost_usd || 0),
    inTok: Number(r.input_tokens || 0),
    outTok: Number(r.output_tokens || 0),
    cacheRead: Number(r.cache_read_tokens || 0),
    cacheWrite: Number(r.cache_write_tokens || 0),
    cacheSaved: Number(r.cache_savings_usd || 0),
  }));
  const totalSpend = series.reduce((a, r) => a + r.spend, 0);
  const totalTok = rows.reduce((a, r) => a + (r.input_tokens || 0) + (r.output_tokens || 0), 0);
  const totalCacheSaved = rows.reduce((a, r) => a + (r.cache_savings_usd || 0), 0);
  const avg = series.length ? totalSpend / series.length : 0;
  const hasCache = series.some((r) => r.cacheRead > 0 || r.cacheWrite > 0);

  if (series.length === 0) {
    return <div className="notice info">No historical data in this window yet. Analytics fills in as developers send real traffic (records land within ~1-2 min of a request).</div>;
  }
  return (
    <>
      <div className="stat-grid">
        <Metric label="Total spend (range)" value={fmtUsd(totalSpend)} />
        <Metric label="Total tokens" value={fmtTok(totalTok)} />
        <Metric label="Avg / day" value={fmtUsd(avg)} />
        <Metric label="Cache saved (range)" value={fmtUsd(totalCacheSaved)} accent="var(--tier1)" />
      </div>
      <div className="panel">
        <h2>Daily spend</h2>
        <ResponsiveContainer width="100%" height={280}>
          <AreaChart data={series}>
            <defs>
              <linearGradient id="agrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="var(--accent)" stopOpacity={0.35} />
                <stop offset="95%" stopColor="var(--accent)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
            <XAxis dataKey="day" stroke="var(--muted)" fontSize={11} axisLine={false} tickLine={false} />
            <YAxis stroke="var(--muted)" fontSize={11} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} />
            <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
            <Area type="monotone" dataKey="spend" name="Spend" stroke="var(--accent)" strokeWidth={2} fill="url(#agrad)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div className="panel">
        <h2>Daily tokens (input · output · cache)</h2>
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={series}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
            <XAxis dataKey="day" stroke="var(--muted)" fontSize={11} axisLine={false} tickLine={false} />
            <YAxis stroke="var(--muted)" fontSize={11} axisLine={false} tickLine={false} tickFormatter={(v) => `${(v / 1000).toFixed(0)}k`} />
            <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtTok(v)} />
            <Legend />
            <Bar dataKey="inTok" name="Input" stackId="t" fill="var(--accent)" />
            <Bar dataKey="outTok" name="Output" stackId="t" fill="var(--accent-2)" />
            <Bar dataKey="cacheRead" name="Cache read" stackId="t" fill="#10b981" />
            <Bar dataKey="cacheWrite" name="Cache write" stackId="t" fill="#f59e0b" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      {hasCache && (
        <div className="panel">
          <h2>Daily prompt-cache savings</h2>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={series}>
              <defs>
                <linearGradient id="csgrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#10b981" stopOpacity={0.35} />
                  <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
              <XAxis dataKey="day" stroke="var(--muted)" fontSize={11} axisLine={false} tickLine={false} />
              <YAxis stroke="var(--muted)" fontSize={11} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} />
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
              <Area type="monotone" dataKey="cacheSaved" name="Cache saved" stroke="#10b981" strokeWidth={2} fill="url(#csgrad)" />
            </AreaChart>
          </ResponsiveContainer>
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
            Dollars avoided by serving prompt-cache reads at the reduced cache rate instead of the full input rate.
          </p>
        </div>
      )}
    </>
  );
}

function AnalyticsBreakdown({ rows, dim }) {
  // Model dim keys on `model`; the tag dims key on `key`.
  const label = dim === "model" ? "Model" : dim === "cost_center" ? "Cost center" : dim === "group" ? "Group" : "Developer";
  const nameOf = (r) => (dim === "model" ? r.model : r.key);
  const sorted = [...rows].sort((a, b) => (b.cost_usd || 0) - (a.cost_usd || 0));
  const chart = sorted.map((r) => ({ name: dim === "model" ? shortModel(nameOf(r)) : nameOf(r), value: Number(r.cost_usd || 0) }));
  const totalSpend = sorted.reduce((a, r) => a + (r.cost_usd || 0), 0);
  const totalTok = sorted.reduce((a, r) => a + (r.input_tokens || 0) + (r.output_tokens || 0), 0);
  const totalCacheSaved = sorted.reduce((a, r) => a + (r.cache_savings_usd || 0), 0);
  const hasCache = sorted.some((r) => (r.cache_read_tokens || 0) > 0 || (r.cache_write_tokens || 0) > 0);

  if (sorted.length === 0) {
    return <div className="notice info">No data for this breakdown in the selected window yet.</div>;
  }
  return (
    <>
      <div className="stat-grid">
        <Metric label={`Total spend (${label.toLowerCase()}s)`} value={fmtUsd(totalSpend)} />
        <Metric label="Total tokens" value={fmtTok(totalTok)} />
        <Metric label="Cache saved" value={fmtUsd(totalCacheSaved)} accent="var(--tier1)" />
        <Metric label={`${label}s`} value={sorted.length} />
      </div>
      <div className="grid-2">
        <div className="panel">
          <h2>Spend by {label.toLowerCase()}</h2>
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={chart} layout="vertical" margin={{ left: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" horizontal={false} />
              <XAxis type="number" stroke="var(--muted)" fontSize={11} tickFormatter={(v) => `$${v}`} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="name" stroke="var(--muted)" fontSize={10} width={140} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
              <Bar dataKey="value" radius={[0, 5, 5, 0]}>
                {chart.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="panel">
          <h2>Share</h2>
          <ResponsiveContainer width="100%" height={280}>
            <PieChart>
              <Pie data={chart} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={60} outerRadius={95} paddingAngle={3}>
                {chart.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />)}
              </Pie>
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="panel">
        <h2>{label} detail</h2>
        <table>
          <thead>
            <tr><th>{label}</th><th>Input tokens</th><th>Output tokens</th>
              {hasCache && <th>Cache read</th>}{hasCache && <th>Cache write</th>}{hasCache && <th>Cache saved</th>}
              <th>Spend</th>{dim === "model" && <th>Pricing</th>}</tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => (
              <tr key={i}>
                <td>{dim === "model" ? <code>{nameOf(r)}</code> : <b>{nameOf(r)}</b>}</td>
                <td>{fmtTok(r.input_tokens)}</td>
                <td>{fmtTok(r.output_tokens)}</td>
                {hasCache && <td>{fmtTok(r.cache_read_tokens)}</td>}
                {hasCache && <td>{fmtTok(r.cache_write_tokens)}</td>}
                {hasCache && <td className="ok">{fmtUsd(r.cache_savings_usd)}</td>}
                <td>{fmtUsd(r.cost_usd)}</td>
                {dim === "model" && <td>{r.priced ? <span className="badge ok">priced</span> : <span className="badge warning">est.</span>}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ============================================================ CONFIGURE · Projects
function ProjectsView({ demo }) {
  const { data, err, loading, reload } = useData(demo, api.listProjects, DEMO_PROJECTS);
  const models = useData(demo, api.listModels, DEMO_MODELS);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState(null);
  const [query, setQuery] = useState("");
  const projects = (data && data.projects) || [];
  const modelList = (models.data && models.data.models) || [];

  const onDelete = async (id) => {
    if (demo) return;
    if (!window.confirm(`Delete project ${id}? This removes its config (developers must be reassigned).`)) return;
    try { await api.deleteProject(id); reload(); }
    catch (e) { alert(e.message); }
  };

  const q = query.trim().toLowerCase();
  const filtered = q
    ? projects.filter((p) =>
        (p.name || "").toLowerCase().includes(q) ||
        (p.cost_center || "").toLowerCase().includes(q) ||
        (p.group || "").toLowerCase().includes(q))
    : projects;

  const closeForms = () => { setShowForm(false); setEditing(null); };

  return (
    <>
      <p className="sub">A customer project is the unit of budget + model policy. Developers are assigned to a project; each developer gets their own mantle project for per-developer cost tracking, and the interceptor resolves their virtual model to a tier by budget usage.</p>

      <div className="row" style={{ marginBottom: 14, gap: 10 }}>
        <input placeholder="Search by project, cost center, or group…" value={query}
               onChange={(e) => setQuery(e.target.value)} style={{ maxWidth: 340 }} />
        <div className="spacer" style={{ flex: 1 }} />
        <button className="btn" onClick={() => { setEditing(null); setShowForm((s) => !s); }} disabled={demo}>
          {showForm ? "Close" : "+ New project"}
        </button>
      </div>

      {demo && <div className="notice info">Demo mode is read-only. Switch to Live (with an admin login) to create or edit projects.</div>}
      <Status loading={loading} err={err} />

      {showForm && !demo && (
        <ProjectForm models={modelList} onDone={() => { closeForms(); reload(); }} onCancel={closeForms} />
      )}
      {editing && !demo && (
        <ProjectForm models={modelList} project={editing}
                     onDone={() => { closeForms(); reload(); }} onCancel={closeForms} />
      )}

      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Project</th><th>Group</th><th>Cost center</th><th>Budget/day</th><th>Devs</th>
              <th><span className="badge tier1">Tier 1</span></th>
              <th><span className="badge tier2">Tier 2</span></th>
              <th><span className="badge tier3">Tier 3</span></th>
              <th>Thresholds</th><th></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((p) => {
              const th = p.thresholds || [75, 90, 100];
              return (
                <tr key={p.project_id}>
                  <td><b>{p.name}</b><div className="mono muted">{p.project_id}</div></td>
                  <td>{p.group}</td>
                  <td>{p.cost_center}</td>
                  <td>{fmtUsd(p.daily_budget_usd)}</td>
                  <td>{(p.developers || []).length}</td>
                  <td><code>{p.tier1_model || "—"}</code></td>
                  <td><code>{p.tier2_model || "—"}</code></td>
                  <td><code>{p.tier3_model || "—"}</code></td>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>{th.join(" / ")}%</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button className="btn secondary sm" onClick={() => { setShowForm(false); setEditing(p); }}
                            disabled={demo}>Update</button>{" "}
                    <button className="btn danger sm" onClick={() => onDelete(p.project_id)} disabled={demo}>Delete</button>
                  </td>
                </tr>
              );
            })}
            {!loading && filtered.length === 0 && (
              <tr><td colSpan={10} className="muted">
                {projects.length === 0 ? "No projects yet." : "No projects match your search."}
              </td></tr>
            )}
          </tbody>
        </table>
        <p className="muted mt" style={{ fontSize: 12 }}>
          Thresholds are the budget % where a developer's model is downgraded: Tier 1 below the
          first, Tier 2 between the first two, Tier 3 up to the last, then blocked (403).
        </p>
      </div>
    </>
  );
}

function ProjectForm({ models, project, onDone, onCancel }) {
  const editing = Boolean(project);
  const [f, setF] = useState({
    name: project?.name || "",
    group: project?.group || "default",
    cost_center: project?.cost_center || "unassigned",
    daily_budget_usd: project?.daily_budget_usd ?? 10,
    allowed_models: project?.allowed_models || [],
    tier1_model: project?.tier1_model || "",
    tier2_model: project?.tier2_model || "",
    tier3_model: project?.tier3_model || "",
    thresholds: (project?.thresholds || [75, 90, 100]).map(Number),
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const ids = models.map((m) => m.model_id);
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const payload = { ...f, daily_budget_usd: Number(f.daily_budget_usd) };
      if (editing) await api.updateProject(project.project_id, payload);
      else await api.createProject(payload);
      onDone();
    } catch (e2) { setErr(e2.message); }
    finally { setBusy(false); }
  };

  return (
    <form className="panel accent-edge" onSubmit={submit}>
      <h2>{editing ? `Update project · ${project.name}` : "New project"}</h2>
      <div className="grid-2">
        <label className="field"><span>Name</span>
          <input value={f.name} onChange={(e) => set("name", e.target.value)} required /></label>
        <label className="field"><span>Daily budget (USD)</span>
          <input type="number" min="0" step="0.5" value={f.daily_budget_usd}
                 onChange={(e) => set("daily_budget_usd", e.target.value)} /></label>
        <label className="field"><span>Group</span>
          <input value={f.group} onChange={(e) => set("group", e.target.value)} /></label>
        <label className="field"><span>Cost center</span>
          <input value={f.cost_center} onChange={(e) => set("cost_center", e.target.value)} /></label>
      </div>
      <div className="grid-2">
        <label className="field"><span>Tier 1 model (healthy budget)</span>
          <ModelSelect ids={ids} value={f.tier1_model} onChange={(v) => set("tier1_model", v)} /></label>
        <label className="field"><span>Tier 2 model (warning)</span>
          <ModelSelect ids={ids} value={f.tier2_model} onChange={(v) => set("tier2_model", v)} /></label>
        <label className="field"><span>Tier 3 model (critical)</span>
          <ModelSelect ids={ids} value={f.tier3_model} onChange={(v) => set("tier3_model", v)} /></label>
        <label className="field"><span>Thresholds (tier2 / tier3 / block %)</span>
          <input value={f.thresholds.join(",")}
                 onChange={(e) => set("thresholds", e.target.value.split(",").map((n) => Number(n.trim())))} /></label>
      </div>
      <label className="field"><span>Allowed models (safety guard — ctrl/cmd-click to multi-select)</span>
        <select multiple value={f.allowed_models}
                onChange={(e) => set("allowed_models", Array.from(e.target.selectedOptions).map((o) => o.value))}
                style={{ minHeight: 120 }}>
          {ids.map((id) => <option key={id} value={id}>{id}</option>)}
        </select>
      </label>
      <div className="row end" style={{ gap: 10 }}>
        <button type="button" className="btn secondary" onClick={onCancel} disabled={busy}>Cancel</button>
        <button className="btn" type="submit" disabled={busy}>
          {busy ? (editing ? "Saving…" : "Creating…") : (editing ? "Save changes" : "Create project")}
        </button>
      </div>
      {err && <div className="err">{err}</div>}
    </form>
  );
}

function ModelSelect({ ids, value, onChange }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">-- select --</option>
      {ids.map((id) => <option key={id} value={id}>{id}</option>)}
    </select>
  );
}

// ============================================================ CONFIGURE · Developers
function DevelopersView({ demo }) {
  const users = useData(demo, api.listUsers, DEMO_USERS);
  const projects = useData(demo, api.listProjects, DEMO_PROJECTS);
  const [sel, setSel] = useState({});
  const [msg, setMsg] = useState("");
  const userList = (users.data && users.data.users) || [];
  const projList = (projects.data && projects.data.projects) || [];

  const currentByUser = {};
  projList.forEach((p) => (p.developers || []).forEach((d) => {
    currentByUser[d.sub] = { project: p.name, projectId: p.project_id, mantle: d.mantle_project_id };
  }));

  const [busy, setBusy] = useState({});
  const assign = async (u) => {
    const project_id = sel[u.sub];
    if (!project_id) return;
    if (demo) { setMsg(`(demo) would assign ${u.email} → ${project_id}`); return; }
    setBusy((b) => ({ ...b, [u.sub]: true }));
    setMsg("");
    try {
      const r = await api.assignDeveloper({ sub: u.sub, project_id, email: u.email });
      setMsg(`Assigned ${u.email} → ${project_id} (mantle project ${r.mantle_project_id})`);
      projects.reload();
    } catch (e) { setMsg(e.message); }
    finally { setBusy((b) => ({ ...b, [u.sub]: false })); }
  };

  return (
    <>
      <p className="sub">Assign Cognito users to a customer project. On first assignment each developer gets their OWN bedrock-mantle project (their permanent per-developer cost identity); the project's budget + tier policy is applied to them, and the interceptor resolves everything in a single lookup.</p>
      <Status loading={users.loading || projects.loading} err={users.err || projects.err} />
      {msg && <div className="notice info">{msg}</div>}
      <div className="panel">
        <table>
          <thead><tr><th>User</th><th>Email</th><th>Status</th><th>Current project</th><th>Mantle project (cost id)</th><th>Assign to</th><th></th></tr></thead>
          <tbody>
            {userList.map((u) => {
              const cur = currentByUser[u.sub];
              return (
              <tr key={u.sub || u.username}>
                <td>{u.username}</td><td>{u.email}</td>
                <td>{u.status === "CONFIRMED"
                  ? <span className="badge ok">confirmed</span>
                  : <span className="badge warning">{(u.status || "").toLowerCase()}</span>}</td>
                <td>{cur ? cur.project : <span className="muted">unassigned</span>}</td>
                <td>{cur && cur.mantle
                  ? <span className="mono muted">{cur.mantle}</span>
                  : <span className="muted">—</span>}</td>
                <td style={{ minWidth: 200 }}>
                  <select value={sel[u.sub] || ""} onChange={(e) => setSel((s) => ({ ...s, [u.sub]: e.target.value }))}>
                    <option value="">-- project --</option>
                    {projList.map((p) => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}
                  </select>
                </td>
                <td><button className="btn secondary sm" onClick={() => assign(u)}
                            disabled={!sel[u.sub] || busy[u.sub]}>
                  {busy[u.sub] ? "Assigning…" : "Assign"}</button></td>
              </tr>
              );
            })}
            {userList.length === 0 && <tr><td colSpan={7} className="muted">No users.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ============================================================ CONFIGURE · Models
function ModelsView({ demo }) {
  const { data, err, loading, reload } = useData(demo, api.listModels, DEMO_MODELS);
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState("");
  const models = (data && data.models) || [];

  const refresh = async () => {
    if (demo) { setMsg("(demo) refresh would invoke the pricing-refresh Lambda"); return; }
    setRefreshing(true); setMsg("");
    try {
      await api.refreshModels();
      setMsg("Refresh started. Pricing updates in the background; reload in ~1 min.");
    } catch (e) { setMsg(e.message); }
    finally { setRefreshing(false); }
  };

  // Demo rows may not carry price_available; treat "has prices" as priced.
  const isPriced = (m) => m.price_available !== false && m.input_price_per_1k != null && m.output_price_per_1k != null;

  return (
    <>
      <p className="sub">Routable models in {REGION} (from the mantle catalog + Bedrock pricing). Only priced models are recommended for a project tier.</p>
      <div className="row end" style={{ marginBottom: 14, gap: 10 }}>
        <button className="btn secondary" onClick={reload} disabled={loading}>Reload</button>
        <button className="btn" onClick={refresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh models"}
        </button>
      </div>
      {msg && <div className="notice info">{msg}</div>}
      <Status loading={loading} err={err} />
      <div className="panel">
        <table>
          <thead><tr><th>Model (routable id)</th><th>Vendor</th><th>Input $/1k</th><th>Output $/1k</th><th>Pricing</th></tr></thead>
          <tbody>
            {models.map((m) => {
              const priced = isPriced(m);
              const vendor = (m.model_id.split(".")[0] || "").toUpperCase();
              return (
                <tr key={m.model_id}>
                  <td className="mono">{m.model_id}</td>
                  <td><span className="vendor-pill">{vendor}</span></td>
                  <td>{priced ? `$${Number(m.input_price_per_1k).toFixed(5)}` : <span className="muted">n/a</span>}</td>
                  <td>{priced ? `$${Number(m.output_price_per_1k).toFixed(5)}` : <span className="muted">n/a</span>}</td>
                  <td>{priced
                    ? <span className="badge ok">priced</span>
                    : <span className="badge warning">no list price</span>}</td>
                </tr>
              );
            })}
            {models.length === 0 && <tr><td colSpan={5} className="muted">No models. Try Refresh.</td></tr>}
          </tbody>
        </table>
        <p className="muted mt" style={{ fontSize: 12 }}>
          All models are routable through the gateway. "No list price" means AWS hasn't published
          an on-demand token price for that model yet (common for the newest premium models);
          they remain selectable and budget cost falls back to a conservative rate.
        </p>
      </div>
    </>
  );
}

// ============================================================ GOVERNANCE · Enforcement
function EnforcementView({ demo }) {
  const { data, err, loading } = useData(demo, api.getEnforcement, DEMO_ENFORCEMENT);
  const rows = (data && data.enforcement) || [];
  const byState = rows.reduce((a, r) => { a[r.state] = (a[r.state] || 0) + 1; return a; }, {});
  return (
    <>
      <p className="sub">Live tiering state per developer. As a project's budget is consumed, the interceptor rewrites the incoming virtual model down the tier ladder; at 100% requests are blocked.</p>
      <div className="stat-grid">
        <Metric label="Tier 1 (healthy)" value={byState.tier1 || 0} accent="var(--tier1)" />
        <Metric label="Tier 2 (warning)" value={byState.tier2 || 0} accent="var(--tier2)" />
        <Metric label="Tier 3 (critical)" value={byState.tier3 || 0} accent="var(--tier3)" />
        <Metric label="Blocked" value={byState.blocked || 0} accent="var(--blocked)" />
      </div>
      <Status loading={loading} err={err} />
      <div className="panel">
        <table>
          <thead><tr><th>Developer</th><th>Customer project</th><th>Group</th><th>Budget %</th><th>State</th><th>Resolved model</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{r.developer}</td>
                <td>{r.customer_project_name || r.customer_project}</td>
                <td>{r.group}</td>
                <td style={{ width: 160 }}><BudgetBar pct={r.budget_pct} /></td>
                <td><span className={`badge ${r.state}`}>{r.state}</span></td>
                <td>{r.blocked
                  ? <span className="muted">blocked</span>
                  : <code>{r.resolved_model || "—"}</code>}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={6} className="muted">No developers assigned.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ============================================================ GOVERNANCE · Alerts
function AlertsView({ demo }) {
  const { data, err, loading } = useData(demo, api.getAlerts, DEMO_ALERTS);
  const alerts = (data && data.alerts) || [];
  return (
    <>
      <p className="sub">Developers at or above 75% of their daily budget. Derived on the fly from live per-developer budget state — no separate alert store.</p>
      <Status loading={loading} err={err} />
      {alerts.length === 0 && !loading && <div className="notice info">No active alerts. All developers are under 75% of budget.</div>}
      {alerts.map((a, i) => (
        <div className="panel alert-card" key={i} style={{ borderLeftColor: a.type === "BLOCKED" ? "var(--blocked)" : "var(--tier2)" }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div>
              <span className={`badge ${a.type === "BLOCKED" ? "blocked" : "warning"}`}>{a.type}</span>
              &nbsp;<b>{a.developer}</b> <span className="muted">· {a.customer_project_name || a.customer_project} · {a.group} · {a.cost_center}</span>
            </div>
            <div className="muted">{a.updated_at ? new Date(a.updated_at).toLocaleString() : ""}</div>
          </div>
          <div className="mt" style={{ maxWidth: 420 }}><BudgetBar pct={a.budget_pct} /></div>
          <div className="muted mt">Spend today: {fmtUsd(a.cost_today_usd)} · {Number(a.budget_pct || 0).toFixed(1)}% of budget</div>
        </div>
      ))}
    </>
  );
}

// ============================================================ SYSTEM · Architecture
function ArchitectureView() {
  const [tab, setTab] = useState("flow");
  const pipeline = [
    { id: "ide", label: "IDE / Portal", sub: "virtual model + JWT", color: "#64748b" },
    { id: "gw", label: "AgentCore Gateway", sub: "CUSTOM_JWT (Cognito)", color: "#0ea5a4" },
    { id: "int", label: "Interceptor", sub: "sub → tier model", color: "#7c5cff" },
    { id: "cedar", label: "Cedar Policy", sub: "permit + forbid", color: "#f59e0b" },
    { id: "mantle", label: "bedrock-mantle", sub: "resolved model", color: "#6366f1" },
    { id: "cw", label: "CloudWatch", sub: "per-project metrics", color: "#38bdf8" },
  ];
  const components = [
    {
      category: "Inbound & Auth", color: "#0ea5a4",
      items: [
        { name: "AgentCore Gateway", desc: "OpenAI-compatible /inference endpoint", detail: "CUSTOM_JWT authorizer (Cognito), allowedClients + allowedScopes; interceptor + policy engine attached" },
        { name: "Cognito User Pool", desc: "In-app auth (no Hosted UI redirect)", detail: "admins group gates /admin/*; developers authenticate their IDE/portal with their access token" },
        { name: "HTTP API (API Gateway v2)", desc: "Admin + developer REST API", detail: "JWT authorizer; single routing Lambda; /admin/* (admins) and /me/* (self)" },
      ],
    },
    {
      category: "Request path", color: "#7c5cff",
      items: [
        { name: "Interceptor Lambda", desc: "Per-developer mapping + model resolution", detail: "Decodes JWT sub → single DynamoDB lookup (config + budget) → injects OpenAI-Project + budget_exceeded → rewrites virtual model to the tier model by budget %" },
        { name: "Cedar Policy Engine", desc: "Deny-by-default authorization", detail: "PermitGatewayRequests permits normal calls; ForbidBudgetExceeded blocks when budget_exceeded=true (forbid overrides permit)" },
        { name: "bedrock-mantle connector", desc: "Inference target", detail: "Resolved request routed under the developer's own mantle project; emits AWS/BedrockMantle metrics (Project + Model dims)" },
      ],
    },
    {
      category: "Governance loop", color: "#f59e0b",
      items: [
        { name: "Aggregator Lambda", desc: "Every 5 min (EventBridge)", detail: "GetMetricData per model since midnight → cost via pricing table → overwrites each developer's Budget row (idempotent, self-healing) with budget_pct + budget_exceeded" },
        { name: "Pricing Refresh Lambda", desc: "Daily / manual", detail: "Lists routable mantle models + Bedrock pricing, marks price_available in the Pricing table" },
        { name: "Cross-family tiering", desc: "Premium → mid → open-source", detail: "Each project defines tier1/2/3 model ids + thresholds; interceptor picks the tier for the developer's current budget %" },
      ],
    },
    {
      category: "Analytics & storage", color: "#38bdf8",
      items: [
        { name: "CloudWatch Metric Stream", desc: "Token metrics → Firehose", detail: "Streams AWS/BedrockMantle token metrics for enrichment" },
        { name: "Enrich Lambda (Firehose)", desc: "Tag stamping", detail: "Stamps CostCenter / Group / Developer from the config table onto each metric record" },
        { name: "S3 analytics bucket", desc: "Athena-ready cold storage", detail: "GZIP JSONL, partitioned, lifecycle-managed for historical per-cost-center analysis" },
      ],
    },
  ];
  const tables = [
    { name: "Config", color: "#0ea5a4", desc: "Customer projects (policy-only) + per-developer assignments",
      keys: "PK sub | __project__:<uuid>", attrs: ["name", "group", "cost_center", "daily_budget_usd", "tier1/2/3_model", "thresholds", "customer_project", "mantle_project_id"] },
    { name: "Budget", color: "#7c5cff", desc: "Per-developer daily budget state (overwritten by aggregator)",
      keys: "PK project_id (= per-dev mantle id)", attrs: ["cost_today_usd", "budget_pct", "budget_exceeded", "input_tokens", "output_tokens", "updated_at"] },
    { name: "Pricing", color: "#f59e0b", desc: "Model catalog + token pricing cache",
      keys: "PK model_id", attrs: ["input_price_per_1k", "output_price_per_1k", "price_available", "vendor", "updated_at"] },
  ];
  const states = [
    { state: "TIER 1", color: "var(--tier1)", threshold: "< tier2 %", desc: "Healthy budget", action: "Premium model (e.g. GPT-5 / Claude Opus)" },
    { state: "TIER 2", color: "var(--tier2)", threshold: "tier2 – tier3 %", desc: "Warning", action: "Mid model (e.g. Claude Haiku / DeepSeek)" },
    { state: "TIER 3", color: "var(--tier3)", threshold: "tier3 – block %", desc: "Critical", action: "Open-source model (e.g. Gemma / Qwen)" },
    { state: "BLOCKED", color: "var(--blocked)", threshold: "≥ block %", desc: "Budget exhausted", action: "Cedar forbid → 403; resets at midnight UTC" },
  ];

  return (
    <>
      <p className="sub">How a coding-IDE request flows through Tokenomics governance, and the resources behind it.</p>
      <div className="tabs">
        {[["flow", "Components & Flow"], ["data", "Data Model"], ["enf", "Enforcement"]].map(([id, label]) => (
          <button key={id} className={`tab ${tab === id ? "active" : ""}`} onClick={() => setTab(id)}>{label}</button>
        ))}
      </div>

      {tab === "flow" && (
        <>
          <div className="panel">
            <h2>Request lifecycle</h2>
            <div className="pipeline">
              {pipeline.map((n, i) => (
                <React.Fragment key={n.id}>
                  <div className="pipe-node" style={{ "--nc": n.color }}>
                    <div className="pipe-dot" style={{ background: n.color }} />
                    <div className="pipe-label">{n.label}</div>
                    <div className="pipe-sub">{n.sub}</div>
                  </div>
                  {i < pipeline.length - 1 && <div className="pipe-arrow">▸</div>}
                </React.Fragment>
              ))}
            </div>
          </div>
          <div className="arch-grid">
            {components.map((cat) => (
              <div key={cat.category} className="panel arch-cat">
                <div className="arch-cat-head" style={{ "--cc": cat.color }}>
                  <span className="dot" style={{ background: cat.color }} />{cat.category}
                </div>
                {cat.items.map((it, i) => (
                  <div key={i} className="arch-item">
                    <div className="arch-item-name">{it.name}</div>
                    <div className="arch-item-desc">{it.desc}</div>
                    <div className="arch-item-detail">{it.detail}</div>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </>
      )}

      {tab === "data" && (
        <>
          <div className="arch-grid">
            {tables.map((t) => (
              <div key={t.name} className="panel ddb-card" style={{ "--tc": t.color }}>
                <div className="ddb-name">DynamoDB · {t.name}</div>
                <div className="ddb-desc">{t.desc}</div>
                <div className="ddb-keys"><code>{t.keys}</code></div>
                <div className="ddb-attrs">
                  {t.attrs.map((a) => <code key={a} className="ddb-attr">{a}</code>)}
                </div>
              </div>
            ))}
          </div>
          <div className="panel">
            <h2>Per-developer cost model</h2>
            <p className="muted" style={{ marginTop: 0 }}>
              A customer project (<code>cproj_…</code>) is policy-only. Each developer gets their own
              permanent mantle project (<code>proj_…</code>) on assignment — the unit bedrock-mantle
              meters against. Reassignment updates that project's tags (Developer / Project / Group /
              CostCenter) in place, so per-developer cost history survives team moves. Customer-project
              spend is the sum of its developers' per-developer costs.
            </p>
          </div>
        </>
      )}

      {tab === "enf" && (
        <div className="panel">
          <h2>Progressive tiering</h2>
          <div className="state-machine">
            {states.map((s, i) => (
              <React.Fragment key={s.state}>
                <div className="state-node" style={{ "--sc": s.color }}>
                  <div className="state-name" style={{ color: s.color }}>{s.state}</div>
                  <div className="state-threshold">{s.threshold}</div>
                  <div className="state-desc">{s.desc}</div>
                  <div className="state-action">{s.action}</div>
                </div>
                {i < states.length - 1 && <div className="state-arrow">▸</div>}
              </React.Fragment>
            ))}
          </div>
          <p className="muted mt">
            The interceptor computes the tier from the developer's live <code>budget_pct</code> and
            rewrites the incoming virtual model to that tier's concrete model. At the block threshold
            the Cedar <code>ForbidBudgetExceeded</code> policy denies the request (403) until the daily
            budget resets.
          </p>
        </div>
      )}
    </>
  );
}

// ============================================================ SYSTEM · API Explorer
const API_CATEGORIES = [
  {
    name: "Projects", desc: "Customer projects are policy-only (budget, tier ladder, thresholds).",
    endpoints: [
      { method: "GET", path: "/admin/projects", desc: "List customer projects", live: () => api.listProjects() },
      { method: "POST", path: "/admin/projects", desc: "Create a customer project (policy-only)",
        body: { name: "Example", group: "platform", cost_center: "CC-1001", daily_budget_usd: 25, tier1_model: "openai.gpt-5.5", tier2_model: "anthropic.claude-haiku-4-5", tier3_model: "google.gemma-3-4b-it", thresholds: [75, 90, 100] } },
      { method: "PUT", path: "/admin/projects/{id}", desc: "Update a customer project", body: { daily_budget_usd: 30 } },
      { method: "DELETE", path: "/admin/projects/{id}", desc: "Delete a customer project's config" },
    ],
  },
  {
    name: "Developers & Models", desc: "Assignment creates the developer's own mantle project; refresh updates the catalog.",
    endpoints: [
      { method: "GET", path: "/admin/users", desc: "List Cognito users for assignment", live: () => api.listUsers() },
      { method: "POST", path: "/admin/assign", desc: "Assign/reassign a developer to a project", body: { sub: "<cognito-sub>", project_id: "cproj_xxxx", email: "dev@example.com" } },
      { method: "GET", path: "/admin/models", desc: "Model catalog + pricing", live: () => api.listModels() },
      { method: "POST", path: "/admin/refresh-models", desc: "Trigger the pricing-refresh Lambda", live: () => api.refreshModels() },
    ],
  },
  {
    name: "Governance", desc: "Read-only rollups derived from live per-developer budget state.",
    endpoints: [
      { method: "GET", path: "/admin/usage", desc: "Usage rollup (per project + per developer)", live: () => api.getUsage() },
      { method: "GET", path: "/admin/enforcement", desc: "Enforcement state per developer", live: () => api.getEnforcement() },
      { method: "GET", path: "/admin/alerts", desc: "Developers at/over 75% budget", live: () => api.getAlerts() },
    ],
  },
  {
    name: "Developer (self)", desc: "Any authenticated developer, scoped to their own sub.",
    endpoints: [
      { method: "GET", path: "/me/budget", desc: "Caller's own budget state", live: () => api.getMyBudget() },
      { method: "GET", path: "/me/config", desc: "Caller's own project/tier config", live: () => api.getMyConfig() },
    ],
  },
];

function ApiExplorerView({ demo }) {
  const [openKey, setOpenKey] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const total = API_CATEGORIES.reduce((a, c) => a + c.endpoints.length, 0);
  const methodClass = (m) => ({ GET: "get", POST: "post", PUT: "put", DELETE: "delete" }[m] || "get");

  const tryIt = async (ep) => {
    setBusy(true); setResult(null);
    const started = Date.now();
    try {
      if (demo || !ep.live) {
        setResult({ status: 200, elapsed: 0, note: demo ? "Demo mode — live call skipped." : "This endpoint takes a path/body; use the app UI to exercise it safely.", data: ep.body || { ok: true } });
      } else {
        const data = await ep.live();
        setResult({ status: 200, elapsed: Date.now() - started, data });
      }
    } catch (e) {
      setResult({ status: 0, elapsed: Date.now() - started, error: e.message });
    } finally { setBusy(false); }
  };

  return (
    <>
      <p className="sub">The Tokenomics HTTP API. {total} endpoints across <code>/admin/*</code> (admins group) and <code>/me/*</code> (self). Auth is the Cognito idToken sent in the <code>Authorization</code> header.</p>
      <div className="panel api-meta">
        <div className="kv"><div className="k">Base URL</div><div className="v mono">{API_URL || "(set VITE_API_URL)"}</div></div>
        <div className="kv"><div className="k">Auth</div><div className="v">Cognito JWT (idToken) · Authorization header</div></div>
      </div>

      {API_CATEGORIES.map((cat) => (
        <div key={cat.name} className="panel">
          <h2>{cat.name}</h2>
          <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>{cat.desc}</p>
          {cat.endpoints.map((ep, i) => {
            const key = `${cat.name}-${i}`;
            const open = openKey === key;
            return (
              <div key={key} className={`api-ep ${open ? "open" : ""}`}>
                <div className="api-ep-head" onClick={() => { setOpenKey(open ? null : key); setResult(null); }}>
                  <span className={`api-method ${methodClass(ep.method)}`}>{ep.method}</span>
                  <span className="api-path mono">{ep.path}</span>
                  <span className="api-desc">{ep.desc}</span>
                  <span className="api-toggle">{open ? "−" : "+"}</span>
                </div>
                {open && (
                  <div className="api-ep-body">
                    {ep.body && (
                      <div className="api-section">
                        <div className="api-section-title">Example request body</div>
                        <pre className="code-block">{JSON.stringify(ep.body, null, 2)}</pre>
                      </div>
                    )}
                    <button className="btn sm" onClick={() => tryIt(ep)} disabled={busy}>
                      {busy ? "Sending…" : ep.live && !demo ? "Try it (live)" : "Try it"}
                    </button>
                    {result && (
                      <div className="api-result">
                        <div className="api-result-meta">
                          <span className={`badge ${result.status === 200 ? "ok" : "blocked"}`}>{result.status || "ERR"}</span>
                          {result.elapsed ? <span className="muted">{result.elapsed}ms</span> : null}
                          {result.note && <span className="muted">{result.note}</span>}
                        </div>
                        <pre className="code-block">{result.error ? result.error : JSON.stringify(result.data, null, 2)}</pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ))}
    </>
  );
}

// ============================================================ SYSTEM · Cost Analysis
const SCENARIOS = {
  conservative: { label: "Conservative", devs: 50, reqPerDevDay: 60 },
  moderate: { label: "Moderate", devs: 100, reqPerDevDay: 150 },
  heavy: { label: "Heavy (agentic)", devs: 200, reqPerDevDay: 400 },
};

function CostAnalysisView() {
  const [scenario, setScenario] = useState("moderate");
  const sc = SCENARIOS[scenario];
  const reqMonth = sc.devs * sc.reqPerDevDay * 22;

  // Tokenomics infra components (NO Kinesis — Metric Stream -> Firehose).
  const infra = [
    { service: "AgentCore Gateway", basis: "requests + hourly runtime + policy engine", cost: 45 + reqMonth / 1_000_000 * 2, color: "#0ea5a4" },
    { service: "Lambda (5 functions)", basis: "interceptor / enrich / aggregator / pricing / api", cost: 6 + reqMonth / 1_000_000 * 0.8, color: "#7c5cff" },
    { service: "DynamoDB (3 tables)", basis: "on-demand R/W + storage", cost: 5 + reqMonth / 1_000_000 * 1.1, color: "#6366f1" },
    { service: "CloudWatch Metric Stream", basis: "per metric update", cost: 4, color: "#38bdf8" },
    { service: "Kinesis Data Firehose", basis: "per GB ingested", cost: 3, color: "#f59e0b" },
    { service: "S3 (analytics)", basis: "GZIP JSONL + lifecycle", cost: 2, color: "#10b981" },
    { service: "API Gateway + Cognito", basis: "HTTP API + MAU (50k free)", cost: 2, color: "#94a3b8" },
  ];
  const totalInfra = infra.reduce((a, c) => a + c.cost, 0);

  // Governed model spend + savings from progressive tiering (premium -> mid -> OSS).
  const premiumPerReq = 0.06; // $/request at premium tier (blended in/out)
  const bedrockUngoverned = reqMonth * premiumPerReq;
  const savingsPct = 0.42;
  const savings = bedrockUngoverned * savingsPct;
  const roi = ((savings - totalInfra) / totalInfra).toFixed(0);

  const infraChart = infra.map((c) => ({ name: c.service, value: Number(c.cost.toFixed(2)) }));
  const tierSavings = [
    { tier: "Tier 1 · premium", perReq: 0.060, color: "var(--tier1)", note: "GPT-5 / Claude Opus" },
    { tier: "Tier 2 · mid", perReq: 0.012, color: "var(--tier2)", note: "Claude Haiku / DeepSeek (~80% cheaper)" },
    { tier: "Tier 3 · open-source", perReq: 0.003, color: "var(--tier3)", note: "Gemma / Qwen (~95% cheaper)" },
    { tier: "Blocked", perReq: 0, color: "var(--blocked)", note: "no additional cost until reset" },
  ];

  return (
    <>
      <p className="sub">Cost to <b>run</b> the Tokenomics governance stack vs. the model spend it governs. Estimates, {REGION} on-demand pricing — for planning only.</p>
      <div className="scenario-row">
        {Object.entries(SCENARIOS).map(([k, v]) => (
          <button key={k} className={`scenario-btn ${scenario === k ? "active" : ""}`} onClick={() => setScenario(k)}>{v.label}</button>
        ))}
      </div>

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-label">Infrastructure / mo</div>
          <div className="stat-value" style={{ color: "var(--accent)" }}>{fmtUsd(totalInfra)}</div>
          <div className="stat-sub"><span>{sc.devs} devs · {sc.reqPerDevDay}/day</span></div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Ungoverned model spend</div>
          <div className="stat-value" style={{ color: "var(--blocked)" }}>{fmtUsd(bedrockUngoverned)}</div>
          <div className="stat-sub"><span>{fmtTok(reqMonth)} requests/mo</span></div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Est. savings (tiering)</div>
          <div className="stat-value" style={{ color: "var(--tier1)" }}>{fmtUsd(savings)}</div>
          <div className="stat-sub"><span>~42% via downgrade + block</span></div>
        </div>
        <div className="stat-card">
          <div className="stat-label">ROI</div>
          <div className="stat-value" style={{ color: "var(--accent-2)" }}>{roi}×</div>
          <div className="stat-sub"><span>{fmtUsd(totalInfra)} infra → {fmtUsd(savings)} saved</span></div>
        </div>
      </div>

      <div className="grid-2">
        <div className="panel">
          <h2>Infrastructure breakdown</h2>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={infraChart} layout="vertical" margin={{ left: 60 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" horizontal={false} />
              <XAxis type="number" stroke="var(--muted)" fontSize={11} tickFormatter={(v) => `$${v}`} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="name" stroke="var(--muted)" fontSize={10} width={120} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
              <Bar dataKey="value" radius={[0, 5, 5, 0]}>
                {infraChart.map((_, i) => <Cell key={i} fill={infra[i].color} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="panel">
          <h2>Infra vs. governed spend</h2>
          <ResponsiveContainer width="100%" height={240}>
            <PieChart>
              <Pie data={[{ name: "Infra", value: Number(totalInfra.toFixed(2)) }, { name: "Model spend", value: Number(bedrockUngoverned.toFixed(2)) }]}
                   cx="50%" cy="50%" innerRadius={55} outerRadius={85} dataKey="value" startAngle={90} endAngle={-270}>
                <Cell fill="var(--accent)" />
                <Cell fill="var(--blocked)" />
              </Pie>
              <Tooltip contentStyle={tooltipStyle} formatter={(v) => fmtUsd(v)} />
            </PieChart>
          </ResponsiveContainer>
          <div className="muted" style={{ textAlign: "center", fontSize: 12 }}>
            Infra is {((totalInfra / bedrockUngoverned) * 100).toFixed(2)}% of governed spend
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>How tiering saves money</h2>
        <div className="tier-savings">
          {tierSavings.map((t) => (
            <div key={t.tier} className="tier-save-item" style={{ borderLeftColor: t.color }}>
              <div className="tier-save-name">{t.tier}</div>
              <div className="tier-save-rate">{t.perReq === 0 ? "$0.00" : `$${t.perReq.toFixed(3)}`}/req</div>
              <div className="tier-save-note muted">{t.note}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="panel">
        <h2>Line-item breakdown</h2>
        <table>
          <thead><tr><th>Component</th><th>Billing basis</th><th>Est. $/mo</th></tr></thead>
          <tbody>
            {infra.map((c) => (
              <tr key={c.service}>
                <td><span className="dot" style={{ background: c.color }} /> {c.service}</td>
                <td className="muted">{c.basis}</td>
                <td>{fmtUsd(c.cost)}</td>
              </tr>
            ))}
            <tr><td><b>Total</b></td><td /><td><b>{fmtUsd(totalInfra)}</b></td></tr>
          </tbody>
        </table>
        <p className="muted mt" style={{ fontSize: 12 }}>
          Model inference (the tokens developers consume) is billed separately — that's the spend
          this solution governs. Tokenomics uses a CloudWatch Metric Stream → Firehose → S3
          analytics path (no Kinesis Data Streams).
        </p>
      </div>
    </>
  );
}

// ============================================================ DEMO · Simulation
// A client-side walkthrough of the progressive-tiering enforcement lifecycle. Pick a real
// project (its actual thresholds + tier ladder drive the state machine), then push spend up with
// the slider or the "advance" button and watch the served model downgrade tier1 -> tier2 -> tier3
// and finally block at 100%. Nothing is written to the backend -- this only explains what the
// interceptor + Cedar policy do in production.
function SimulationView({ demo }) {
  const { data } = useData(demo, api.listProjects, DEMO_PROJECTS);
  const projects = (data && data.projects) || [];
  const [pid, setPid] = useState("");
  const [spend, setSpend] = useState(0);
  const [timeline, setTimeline] = useState([]);

  // Pick a default project once loaded.
  useEffect(() => {
    if (!pid && projects.length) setPid(projects[0].project_id);
  }, [projects, pid]);

  const proj = projects.find((p) => p.project_id === pid) || {};
  const budget = Number(proj.daily_budget_usd || 15);
  const th = (proj.thresholds || [75, 90, 100]).map(Number);
  const pct = budget > 0 ? Math.min((spend / budget) * 100, 100) : 0;

  // Resolve tier + model from the selected project's real config.
  const tierOf = (p) => {
    if (p >= th[2]) return { state: "blocked", model: null, color: "var(--blocked)" };
    if (p >= th[1]) return { state: "tier3", model: proj.tier3_model, color: "var(--tier3)" };
    if (p >= th[0]) return { state: "tier2", model: proj.tier2_model, color: "var(--tier2)" };
    return { state: "tier1", model: proj.tier1_model, color: "var(--tier1)" };
  };
  const cur = tierOf(pct);

  const log = (state, s, message) =>
    setTimeline((t) => [{ time: new Date().toLocaleTimeString(), state, cost: s.toFixed(2), message }, ...t]);

  const applyTo = (newSpend, note) => {
    const clamped = Math.max(0, Math.min(newSpend, budget));
    const prevState = tierOf(budget > 0 ? (spend / budget) * 100 : 0).state;
    setSpend(clamped);
    const newPctLocal = budget > 0 ? Math.min((clamped / budget) * 100, 100) : 0;
    const next = tierOf(newPctLocal);
    if (note || next.state !== prevState) {
      const msg = next.state === "blocked"
        ? `Budget reached ${th[2]}% — requests BLOCKED (Cedar 403)`
        : next.state === "tier1"
          ? `Usage at ${newPctLocal.toFixed(0)}% — tier 1, serving ${shortModel(next.model)}`
          : `Crossed ${next.state === "tier2" ? th[0] : th[1]}% — downgraded to ${next.state} (${shortModel(next.model)})`;
      log(next.state, clamped, msg);
    }
  };

  const advance = () => {
    // Jump to the next threshold boundary so each tier is clearly demonstrated.
    let target;
    if (pct < th[0]) target = th[0];
    else if (pct < th[1]) target = th[1];
    else if (pct < th[2]) target = th[2];
    else return;
    applyTo(budget * (target / 100), true);
  };
  const reset = () => { setSpend(0); log("tier1", 0, "Daily reset (midnight UTC) — budget cleared, tier 1 restored"); };
  const clear = () => { setTimeline([]); setSpend(0); };

  const gauge = [{ name: "b", value: pct, fill: cur.color }];
  const stages = [
    { state: "tier1", label: `Tier 1 · < ${th[0]}%`, model: proj.tier1_model, color: "var(--tier1)",
      desc: "Full budget headroom. The interceptor serves the premium model." },
    { state: "tier2", label: `Tier 2 · ${th[0]}–${th[1]}%`, model: proj.tier2_model, color: "var(--tier2)",
      desc: "Warning zone. The interceptor rewrites the virtual model to the mid tier." },
    { state: "tier3", label: `Tier 3 · ${th[1]}–${th[2]}%`, model: proj.tier3_model, color: "var(--tier3)",
      desc: "Critical. The interceptor routes to the cost-efficient open-source model." },
    { state: "blocked", label: `Blocked · ≥ ${th[2]}%`, model: null, color: "var(--blocked)",
      desc: "Cedar ForbidBudgetExceeded denies the request (403) until the daily reset." },
  ];

  return (
    <>
      <p className="sub">An interactive walkthrough of progressive tiering. Pick a project, then push its daily spend up — watch the served model downgrade through the project's real tier ladder and block at {th[2]}%. This is a front-end explainer; nothing is sent to the backend.</p>

      <div className="panel">
        <div className="grid-2">
          <label className="field"><span>Project</span>
            <select value={pid} onChange={(e) => { setPid(e.target.value); setSpend(0); setTimeline([]); }}>
              {projects.map((p) => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}
            </select>
          </label>
          <div className="field"><span>Daily budget</span>
            <div className="mono" style={{ fontSize: 18, fontWeight: 700, paddingTop: 4 }}>{fmtUsd(budget)}</div>
          </div>
        </div>
      </div>

      {/* live state */}
      <div className="panel">
        <div className="sim-state">
          <div className="sim-gauge">
            <ResponsiveContainer width={130} height={130}>
              <RadialBarChart cx="50%" cy="50%" innerRadius="66%" outerRadius="100%" data={gauge} startAngle={90} endAngle={-270}>
                <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
                <RadialBar background dataKey="value" cornerRadius={8} />
              </RadialBarChart>
            </ResponsiveContainer>
            <div className="sim-gauge-pct" style={{ color: cur.color }}>{pct.toFixed(0)}%</div>
          </div>
          <div className="sim-info">
            <span className={`badge ${cur.state}`}>{cur.state}</span>
            <div className="sim-spend">{fmtUsd(spend)} <span className="muted">/ {fmtUsd(budget)}</span></div>
            <div className="sim-model">
              Serving:{" "}
              {cur.state === "blocked"
                ? <b style={{ color: "var(--blocked)" }}>ACCESS DENIED (403)</b>
                : <b>{shortModel(cur.model)}</b>}
            </div>
          </div>
        </div>

        {/* threshold track */}
        <div className="sim-track">
          <div className="sim-track-fill" style={{ width: `${pct}%`, background: cur.color }} />
          {[[th[0], "tier2", "var(--tier2)"], [th[1], "tier3", "var(--tier3)"], [th[2], "block", "var(--blocked)"]].map(([at, lbl, col]) => (
            <div key={lbl} className="sim-marker" style={{ left: `${at}%` }}>
              <div className="sim-marker-line" style={{ background: col }} />
              <div className="sim-marker-label">{at}% {lbl}</div>
            </div>
          ))}
        </div>

        {/* slider */}
        <div className="sim-slider-wrap">
          <label className="muted" style={{ fontSize: 12 }}>Adjust spend: {fmtUsd(spend)}</label>
          <input type="range" min="0" max={budget} step="0.01" value={spend} className="sim-slider"
                 onChange={(e) => applyTo(parseFloat(e.target.value))}
                 style={{ "--pct": `${pct}%`, "--col": cur.color }} />
        </div>

        <div className="row" style={{ gap: 10, marginTop: 14 }}>
          <button className="btn" onClick={advance} disabled={cur.state === "blocked"}>Advance to next threshold</button>
          <button className="btn secondary" onClick={reset}>Reset (midnight)</button>
          <button className="btn secondary" onClick={clear}>Clear timeline</button>
        </div>
      </div>

      {/* timeline */}
      {timeline.length > 0 && (
        <div className="panel">
          <h2>Event timeline</h2>
          <div className="sim-timeline">
            {timeline.map((e, i) => (
              <div key={i} className="sim-tl-entry">
                <span className={`sim-tl-dot ${e.state}`} />
                <span className="sim-tl-time">{e.time}</span>
                <span className="sim-tl-msg">{e.message}</span>
                <span className="sim-tl-cost muted">{fmtUsd(Number(e.cost))}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* production mapping */}
      <div className="panel">
        <h2>How this maps to production</h2>
        <div className="sim-explain">
          {stages.map((s) => (
            <div key={s.state} className="sim-explain-item" style={{ borderLeftColor: s.color }}>
              <div className="sim-explain-head">
                <span className={`badge ${s.state}`}>{s.label}</span>
                {s.model && <code>{s.model}</code>}
              </div>
              <div className="muted" style={{ fontSize: 12.5 }}>{s.desc}</div>
            </div>
          ))}
        </div>
        <p className="muted mt" style={{ fontSize: 12 }}>
          In production the developer's live <code>budget_pct</code> (recomputed every ~5 min by the
          aggregator from their per-developer mantle-project metering) drives this exact decision:
          the gateway request interceptor rewrites the virtual model to the tier model, and the Cedar
          <code>ForbidBudgetExceeded</code> policy denies requests once budget is exhausted.
        </p>
      </div>
    </>
  );
}

// ============================================================ small shared bits
function Metric({ label, value, accent }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={accent ? { color: accent } : undefined}>{value}</div>
    </div>
  );
}

function BudgetBar({ pct }) {
  const p = Math.min(Number(pct || 0), 100);
  const color = p >= 100 ? "var(--blocked)" : p >= 90 ? "var(--tier3)" : p >= 75 ? "var(--tier2)" : "var(--tier1)";
  return (
    <div>
      <div className="bar"><div style={{ width: `${p}%`, background: color }} /></div>
      <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{Number(pct || 0).toFixed(1)}%</div>
    </div>
  );
}

const tooltipStyle = { background: "#ffffff", border: "1px solid #e5e9f2", borderRadius: 10, color: "#1a2233", boxShadow: "0 6px 20px rgba(20,30,60,0.1)" };
