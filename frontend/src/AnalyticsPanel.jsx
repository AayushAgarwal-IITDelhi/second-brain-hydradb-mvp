/**
 * Phase 15: lightweight analytics panel.
 *
 * Four compact cards rendered side-by-side (or stacked on narrow
 * viewports via flex-wrap). NO chart library -- numbers + labels +
 * small badges only, matching the minimal-UI constraint from the
 * brief.
 *
 *   1. Overview   -- query/ingest counts + latency
 *   2. Top Topics -- top entities + their co-mentions
 *   3. Insights   -- stale actions, dormant projects, surging entities
 *   4. Recurring  -- "X mentioned N times in the last 7 days" items
 *
 * Each card lazy-fetches its own data on mount + on refresh. All
 * three API calls run in parallel, so the panel is interactive as
 * soon as the first responds. Each card has its own loading + error
 * state so one slow / failed call never blocks the others.
 */

import { useEffect, useState } from "react";

import {
  getAnalyticsInsights,
  getAnalyticsOverview,
  getAnalyticsTopics,
} from "./api.js";


export default function AnalyticsPanel({ onClose }) {
  const [overview, setOverview] = useState(null);
  const [overviewErr, setOverviewErr] = useState("");

  const [topics, setTopics] = useState(null);
  const [topicsErr, setTopicsErr] = useState("");

  const [insights, setInsights] = useState(null);
  const [insightsErr, setInsightsErr] = useState("");

  function refreshAll() {
    setOverview(null); setOverviewErr("");
    setTopics(null);   setTopicsErr("");
    setInsights(null); setInsightsErr("");

    getAnalyticsOverview(7)
      .then((d) => setOverview(d || {}))
      .catch((e) => setOverviewErr(e?.message || "Could not load overview."));

    getAnalyticsTopics({ days: 30, topN: 8 })
      .then((d) => setTopics(d || {}))
      .catch((e) => setTopicsErr(e?.message || "Could not load topics."));

    getAnalyticsInsights()
      .then((d) => setInsights(d || {}))
      .catch((e) => setInsightsErr(e?.message || "Could not load insights."));
  }

  useEffect(() => {
    refreshAll();
  }, []);

  // ESC closes the panel.
  useEffect(() => {
    function onKey(e) { if (e.key === "Escape") onClose?.(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <section
      className="analytics-panel"
      aria-label="Workspace analytics"
    >
      <header className="analytics-panel__header">
        <strong>Workspace analytics</strong>
        <div className="analytics-panel__actions">
          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={refreshAll}
          >
            Refresh
          </button>
          {onClose && (
            <button
              type="button"
              className="btn btn--ghost btn--small"
              onClick={onClose}
              aria-label="Close analytics"
            >
              Close ✕
            </button>
          )}
        </div>
      </header>

      <div className="analytics-panel__grid">
        <OverviewCard overview={overview} error={overviewErr} />
        <TopicsCard   topics={topics}     error={topicsErr} />
        <InsightsCard insights={insights} error={insightsErr} />
        <RecurringCard insights={insights} error={insightsErr} />
      </div>
    </section>
  );
}


// ── Cards ───────────────────────────────────────────────────────────── //

function OverviewCard({ overview, error }) {
  return (
    <article className="analytics-card">
      <h3 className="analytics-card__title">Last 7 days</h3>
      {error && (
        <div className="analytics-card__error">{error}</div>
      )}
      {!error && overview === null && (
        <div className="analytics-card__loading">Loading…</div>
      )}
      {!error && overview && (
        <>
          <Metric
            label="Queries"
            value={overview.query?.count ?? 0}
          />
          <Metric
            label="Empty results"
            value={overview.query?.empty_result_count ?? 0}
            tone={
              (overview.query?.empty_result_count || 0) > 0 ? "warn" : "ok"
            }
          />
          <Metric
            label="Memory hits"
            value={overview.query?.memory_hit_count ?? 0}
          />
          <Metric
            label="P50 latency"
            value={
              overview.query?.p50_latency_ms != null
                ? `${overview.query.p50_latency_ms} ms`
                : "—"
            }
          />
          <Metric
            label="Ingest runs"
            value={overview.ingest?.runs ?? 0}
          />
          <Metric
            label="Messages ingested"
            value={overview.ingest?.messages_uploaded ?? 0}
          />
          {overview.retrieval_failures?.count > 0 && (
            <Metric
              label="Retrieval failures"
              value={overview.retrieval_failures.count}
              tone="warn"
            />
          )}
        </>
      )}
    </article>
  );
}


function TopicsCard({ topics, error }) {
  const entities = (topics && topics.top_entities) || [];
  return (
    <article className="analytics-card">
      <h3 className="analytics-card__title">Top topics (30 days)</h3>
      {error && (
        <div className="analytics-card__error">{error}</div>
      )}
      {!error && topics === null && (
        <div className="analytics-card__loading">Loading…</div>
      )}
      {!error && topics && entities.length === 0 && (
        <div className="analytics-card__empty">No topics yet.</div>
      )}
      {!error && entities.length > 0 && (
        <ul className="analytics-list">
          {entities.map((e) => (
            <li key={`${e.entity_type}:${e.content}`} className="analytics-list__row">
              <span className="analytics-list__primary">
                <strong>{e.content}</strong>
                <span className="analytics-list__type">
                  {e.entity_type}
                </span>
                <span className="analytics-list__count">
                  {e.mentions}×
                </span>
              </span>
              {e.co_mentions && e.co_mentions.length > 0 && (
                <span className="analytics-list__co">
                  with{" "}
                  {e.co_mentions.slice(0, 3).map((c, idx) => (
                    <span key={c.content + idx}>
                      {idx > 0 && ", "}
                      {c.content}
                    </span>
                  ))}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}


function InsightsCard({ insights, error }) {
  const stale = (insights && insights.stale_action_items) || [];
  const dormant = (insights && insights.dormant_projects) || [];
  const surging = (insights && insights.surging_entities) || [];
  const isEmpty = stale.length === 0 && dormant.length === 0 && surging.length === 0;
  return (
    <article className="analytics-card">
      <h3 className="analytics-card__title">Insights</h3>
      {error && (
        <div className="analytics-card__error">{error}</div>
      )}
      {!error && insights === null && (
        <div className="analytics-card__loading">Loading…</div>
      )}
      {!error && insights && isEmpty && (
        <div className="analytics-card__empty">
          Nothing flagged. Things look clean.
        </div>
      )}
      {!error && insights && !isEmpty && (
        <ul className="analytics-list">
          {surging.slice(0, 5).map((s) => (
            <li
              key={`surge:${s.content}`}
              className="analytics-list__row analytics-list__row--surge"
              title="Mentioned at least 2x more than the prior 7 days"
            >
              <span className="analytics-list__primary">
                <strong>↑ {s.content}</strong>
                <span className="analytics-list__hint">
                  {s.recent_mentions} this week
                  {s.prior_mentions > 0
                    ? `, ${s.prior_mentions} prior week`
                    : ", new topic"}
                </span>
              </span>
            </li>
          ))}
          {stale.slice(0, 5).map((a) => (
            <li
              key={`stale:${a.id}`}
              className="analytics-list__row analytics-list__row--warn"
              title={`Stale since ${a.stale_since}`}
            >
              <span className="analytics-list__primary">
                <span className="analytics-list__type">stale</span>
                <strong>{a.content}</strong>
                {a.owner && (
                  <span className="analytics-list__hint">
                    {" "}— owner: {a.owner}
                  </span>
                )}
              </span>
            </li>
          ))}
          {dormant.slice(0, 5).map((p) => (
            <li
              key={`dormant:${p.content}`}
              className="analytics-list__row"
              title={`Last mentioned ${p.last_seen}`}
            >
              <span className="analytics-list__primary">
                <span className="analytics-list__type">dormant</span>
                <strong>{p.content}</strong>
              </span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}


function RecurringCard({ insights, error }) {
  const recurring = (insights && insights.recurring) || [];
  return (
    <article className="analytics-card">
      <h3 className="analytics-card__title">Recurring patterns</h3>
      {error && (
        <div className="analytics-card__error">{error}</div>
      )}
      {!error && insights === null && (
        <div className="analytics-card__loading">Loading…</div>
      )}
      {!error && insights && recurring.length === 0 && (
        <div className="analytics-card__empty">
          No recurring patterns this week.
        </div>
      )}
      {!error && recurring.length > 0 && (
        <ul className="analytics-list">
          {recurring.slice(0, 8).map((r) => (
            <li
              key={`rec:${r.entity_type}:${r.content}`}
              className="analytics-list__row"
            >
              <span className="analytics-list__primary">{r.label}</span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}


// ── Atomic helpers ──────────────────────────────────────────────────── //

function Metric({ label, value, tone }) {
  return (
    <div className={`metric ${tone ? `metric--${tone}` : ""}`}>
      <span className="metric__label">{label}</span>
      <span className="metric__value">{value}</span>
    </div>
  );
}