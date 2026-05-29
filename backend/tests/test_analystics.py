"""
Phase 15: analytics + intelligence tests.

Coverage areas:

  A. Event emit / aggregation (analytics_store)
  B. Memory graph + topic clustering (analytics_intelligence.topic_overview)
  C. Timeline reconstruction (reconstruct_timeline)
  D. Recurring patterns (recurring_patterns)
  E. Proactive insights (proactive_insights)
  F. Routes integration
  G. Workspace isolation
  H. Defensive failure modes

Most tests use a fake "in-memory" Supabase client built per-test so we
can express the underlying rows directly. This keeps each test focused
on the projection logic instead of mocking Supabase chainable APIs.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


TEST_WS = "00000000-0000-0000-0000-00000000aaaa"
OTHER_WS = "00000000-0000-0000-0000-00000000bbbb"


def _iso(days_ago: int = 0, hours_ago: int = 0) -> str:
    """ISO timestamp `n` days/hours in the past, UTC."""
    return (
        datetime.now(timezone.utc)
        - timedelta(days=days_ago, hours=hours_ago)
    ).isoformat()


# ---------------------------------------------------------------------- #
# Fake Supabase clients
# ---------------------------------------------------------------------- #
# These two helpers build the chainable-select mock shape supabase-py
# exposes. Each test that needs a read patches `get_supabase` with one
# of these.


class _SelectChain:
    """Minimal chainable mock: select(...).eq(...).gte(...).in_(...).
    order(...).limit(...).execute() -> {data: [...]}.

    The chain just collects the filter calls and lets the test's
    `rows` callable produce the rows for whichever (table, kinds,
    workspace_id, days) combination the test wires up. For most
    tests we just need to return a fixed list."""
    def __init__(self, rows):
        self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self): return MagicMock(data=self._rows)
    def insert(self, *a, **k):
        # Inserts also chain to execute().
        return self
    def upsert(self, *a, **k):
        return self


def _fake_client_with_table(rows_by_table):
    """rows_by_table = {"analytics_events": [...], "extracted_memories": [...]}"""
    client = MagicMock()
    def table_fn(name):
        return _SelectChain(rows_by_table.get(name, []))
    client.table = MagicMock(side_effect=table_fn)
    return client


# ====================================================================== #
# A. Event emit + aggregation
# ====================================================================== #
class TestEmitEvent:
    def test_invalid_kind_rejected(self):
        from analytics_store import emit_event
        # No supabase call should happen.
        with patch("analytics_store.get_supabase") as mock_get:
            ok = emit_event(
                workspace_id=TEST_WS, kind="not_a_real_kind",
            )
        assert ok is False
        mock_get.assert_not_called()

    def test_blank_workspace_rejected(self):
        from analytics_store import emit_event
        assert emit_event(workspace_id="", kind="query_completed") is False

    def test_valid_event_writes_one_row(self):
        from analytics_store import emit_event
        client = _fake_client_with_table({})
        with patch("analytics_store.get_supabase", return_value=client):
            ok = emit_event(
                workspace_id=TEST_WS,
                kind="query_completed",
                latency_ms=150,
                payload={"sources_count": 3},
            )
        assert ok is True
        client.table.assert_called_with("analytics_events")

    def test_supabase_failure_returns_false_no_raise(self):
        from analytics_store import emit_event
        with patch(
            "analytics_store.get_supabase",
            side_effect=RuntimeError("supabase down"),
        ):
            ok = emit_event(
                workspace_id=TEST_WS, kind="query_completed",
            )
        assert ok is False     # never raises

    def test_negative_latency_coerced_to_zero(self):
        """Defensive: a negative latency shouldn't poison the row."""
        from analytics_store import emit_event
        captured = {}
        class _Capture(_SelectChain):
            def insert(self, row):
                captured["row"] = row
                return self
            def execute(self): return MagicMock(data=[])
        client = MagicMock()
        client.table = MagicMock(return_value=_Capture([]))
        with patch("analytics_store.get_supabase", return_value=client):
            emit_event(
                workspace_id=TEST_WS, kind="query_completed",
                latency_ms=-50,
            )
        assert captured["row"]["latency_ms"] == 0


class TestAggregateQueryStats:
    def _events(self):
        return [
            {"id": "e1", "kind": "query_completed", "source_kind": None,
             "latency_ms": 100, "success": None,
             "payload": {"sources_count": 3, "source_kinds": ["slack"],
                         "memory_hit": False,
                         "retrieval_mode": "default"},
             "created_at": _iso(hours_ago=1)},
            {"id": "e2", "kind": "query_completed", "source_kind": None,
             "latency_ms": 200, "success": None,
             "payload": {"sources_count": 0, "source_kinds": [],
                         "memory_hit": False,
                         "retrieval_mode": "default"},
             "created_at": _iso(hours_ago=2)},
            {"id": "e3", "kind": "query_completed", "source_kind": None,
             "latency_ms": 50, "success": None,
             "payload": {"sources_count": 5,
                         "source_kinds": ["slack", "gmail"],
                         "memory_hit": True,
                         "retrieval_mode": "recency"},
             "created_at": _iso(hours_ago=3)},
        ]

    def test_aggregates_correctly(self):
        from analytics_store import aggregate_query_stats
        client = _fake_client_with_table({"analytics_events": self._events()})
        with patch("analytics_store.get_supabase", return_value=client):
            stats = aggregate_query_stats(workspace_id=TEST_WS, days=7)
        assert stats["count"] == 3
        assert stats["empty_result_count"] == 1
        assert stats["memory_hit_count"] == 1
        assert stats["recency_rerank_count"] == 1
        # by_source: 2 slack-only (e1, e3 has slack), 1 gmail (e3 has gmail),
        # 1 mixed (e3 has 2 kinds).
        assert stats["by_source"]["slack"] == 2
        assert stats["by_source"]["gmail"] == 1
        assert stats["by_source"]["mixed"] == 1
        # Latency stats are present.
        assert stats["avg_latency_ms"] is not None
        assert stats["p50_latency_ms"] is not None
        assert stats["p95_latency_ms"] is not None

    def test_empty_events_returns_zeros(self):
        from analytics_store import aggregate_query_stats
        client = _fake_client_with_table({"analytics_events": []})
        with patch("analytics_store.get_supabase", return_value=client):
            stats = aggregate_query_stats(workspace_id=TEST_WS, days=7)
        assert stats["count"] == 0
        assert stats["avg_latency_ms"] is None


class TestAggregateIngestStats:
    def test_sums_by_source(self):
        from analytics_store import aggregate_ingest_stats
        events = [
            {"id": "i1", "kind": "ingest_completed",
             "source_kind": "gmail", "latency_ms": 5000,
             "success": True,
             "payload": {"messages_uploaded": 12},
             "created_at": _iso(hours_ago=1)},
            {"id": "i2", "kind": "ingest_completed",
             "source_kind": "gmail", "latency_ms": 3000,
             "success": True,
             "payload": {"messages_uploaded": 7},
             "created_at": _iso(hours_ago=4)},
            {"id": "i3", "kind": "ingest_completed",
             "source_kind": "slack", "latency_ms": 2000,
             "success": False,
             "payload": {"messages_uploaded": 0},
             "created_at": _iso(hours_ago=6)},
        ]
        client = _fake_client_with_table({"analytics_events": events})
        with patch("analytics_store.get_supabase", return_value=client):
            stats = aggregate_ingest_stats(workspace_id=TEST_WS, days=7)
        assert stats["runs"] == 3
        assert stats["messages_uploaded"] == 19
        assert stats["failed_runs"] == 1
        assert stats["by_source"]["gmail"]["uploaded"] == 19
        assert stats["by_source"]["gmail"]["runs"] == 2
        assert stats["by_source"]["slack"]["failed"] == 1
        # last_ingest_at is the newest (i1, hours_ago=1).
        assert stats["last_ingest_at"]


# ====================================================================== #
# B. Memory graph + topic clustering
# ====================================================================== #
class TestTopicOverview:
    def _entities(self):
        # Three memory rows for "Kafka" (service) and two for
        # "Rahul" (person), all in two source documents -- so
        # Kafka & Rahul co-occur.
        return [
            {"id": "m1", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_kind": "slack",
             "source_stable_key": "slack:msg:1",
             "source_timestamp": _iso(days_ago=2),
             "created_at": _iso(days_ago=2), "owner": None},
            {"id": "m2", "kind": "entity", "content": "Rahul",
             "entity_type": "person",
             "source_kind": "slack",
             "source_stable_key": "slack:msg:1",
             "source_timestamp": _iso(days_ago=2),
             "created_at": _iso(days_ago=2), "owner": None},
            {"id": "m3", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_kind": "slack",
             "source_stable_key": "slack:msg:2",
             "source_timestamp": _iso(days_ago=1),
             "created_at": _iso(days_ago=1), "owner": None},
            {"id": "m4", "kind": "entity", "content": "Rahul",
             "entity_type": "person",
             "source_kind": "slack",
             "source_stable_key": "slack:msg:2",
             "source_timestamp": _iso(days_ago=1),
             "created_at": _iso(days_ago=1), "owner": None},
            {"id": "m5", "kind": "entity", "content": "kafka",  # case variant
             "entity_type": "service",
             "source_kind": "slack",
             "source_stable_key": "slack:msg:3",
             "source_timestamp": _iso(hours_ago=2),
             "created_at": _iso(hours_ago=2), "owner": None},
        ]

    def test_top_entities_dedupe_case_insensitive(self):
        from analytics_intelligence import topic_overview
        client = _fake_client_with_table(
            {"extracted_memories": self._entities()},
        )
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = topic_overview(workspace_id=TEST_WS, days=30, top_n=5)
        # Kafka counts merged across "Kafka"/"kafka" case variants.
        kafka = next(
            e for e in out["top_entities"] if e["content"].lower() == "kafka"
        )
        assert kafka["mentions"] == 3
        # First-appearance casing wins for display.
        assert kafka["content"] == "Kafka"
        # Rahul co-mentions Kafka exactly twice (in msg:1 and msg:2).
        rahul = next(
            e for e in out["top_entities"] if e["content"] == "Rahul"
        )
        co = [c for c in rahul["co_mentions"] if c["content"] == "Kafka"]
        assert co and co[0]["count"] == 2

    def test_cluster_count_reflects_multi_entity_sources(self):
        from analytics_intelligence import topic_overview
        client = _fake_client_with_table(
            {"extracted_memories": self._entities()},
        )
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = topic_overview(workspace_id=TEST_WS, days=30)
        # msg:1 has 2 entities, msg:2 has 2 entities -> 2 clusters.
        # msg:3 has 1 entity -> not counted.
        assert out["cluster_count"] == 2

    def test_empty_returns_empty(self):
        from analytics_intelligence import topic_overview
        client = _fake_client_with_table({"extracted_memories": []})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = topic_overview(workspace_id=TEST_WS, days=30)
        assert out == {"top_entities": [], "cluster_count": 0}

    def test_supabase_failure_degrades_gracefully(self):
        from analytics_intelligence import topic_overview
        with patch(
            "analytics_intelligence.get_supabase",
            side_effect=RuntimeError("supabase down"),
        ):
            out = topic_overview(workspace_id=TEST_WS, days=30)
        assert out == {"top_entities": [], "cluster_count": 0}


# ====================================================================== #
# C. Timeline reconstruction
# ====================================================================== #
class TestTimeline:
    def test_filters_by_entity_substring_case_insensitive(self):
        from analytics_intelligence import reconstruct_timeline
        rows = [
            {"id": "r1", "kind": "decision",
             "content": "we agreed to use Kafka",
             "source_kind": "slack",
             "source_stable_key": "slack:msg:1",
             "source_timestamp": _iso(days_ago=5),
             "created_at": _iso(days_ago=5)},
            {"id": "r2", "kind": "action_item",
             "content": "fix the kafka lag",
             "source_kind": "gmail",
             "source_stable_key": "gmail:msg:1",
             "source_timestamp": _iso(days_ago=2),
             "created_at": _iso(days_ago=2)},
            {"id": "r3", "kind": "decision",
             "content": "moving to incremental sync",
             "source_kind": "slack",
             "source_stable_key": "slack:msg:2",
             "source_timestamp": _iso(days_ago=1),
             "created_at": _iso(days_ago=1)},
        ]
        client = _fake_client_with_table({"extracted_memories": rows})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = reconstruct_timeline(
                workspace_id=TEST_WS, entity="Kafka", days=30,
            )
        ids = [r["id"] for r in out]
        # r1 and r2 mention kafka (case-insensitive); r3 doesn't.
        assert "r1" in ids and "r2" in ids
        assert "r3" not in ids
        # Chronological ASC: r1 (5 days ago) before r2 (2 days ago).
        assert ids == ["r1", "r2"]

    def test_filters_by_kind_set(self):
        from analytics_intelligence import reconstruct_timeline
        rows = [
            {"id": "r1", "kind": "decision",
             "content": "x",
             "source_stable_key": "k1",
             "source_timestamp": _iso(days_ago=1),
             "created_at": _iso(days_ago=1)},
            {"id": "r2", "kind": "action_item",
             "content": "y",
             "source_stable_key": "k2",
             "source_timestamp": _iso(days_ago=2),
             "created_at": _iso(days_ago=2)},
            {"id": "r3", "kind": "entity",
             "content": "z",
             "source_stable_key": "k3",
             "source_timestamp": _iso(days_ago=3),
             "created_at": _iso(days_ago=3)},
        ]
        client = _fake_client_with_table({"extracted_memories": rows})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = reconstruct_timeline(
                workspace_id=TEST_WS,
                kinds=["decision", "action_item"],
                days=30,
            )
        ids = [r["id"] for r in out]
        assert ids == ["r2", "r1"]   # ASC by source_timestamp


# ====================================================================== #
# D. Recurring patterns
# ====================================================================== #
class TestRecurringPatterns:
    def test_label_includes_window_and_count(self):
        from analytics_intelligence import recurring_patterns
        rows = [
            {"id": f"m{i}", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_kind": "slack",
             "source_stable_key": f"k{i}",
             "source_timestamp": _iso(hours_ago=i * 2),
             "created_at": _iso(hours_ago=i * 2),
             "owner": None}
            for i in range(5)
        ]
        client = _fake_client_with_table({"extracted_memories": rows})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = recurring_patterns(
                workspace_id=TEST_WS, days=7, min_mentions=3,
            )
        assert len(out) == 1
        assert out[0]["count"] == 5
        assert "Kafka" in out[0]["label"]
        assert "5 times" in out[0]["label"]
        assert "7 days" in out[0]["label"]

    def test_threshold_excludes_low_count_entities(self):
        from analytics_intelligence import recurring_patterns
        rows = [
            {"id": "m1", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_stable_key": "k1",
             "source_timestamp": _iso(hours_ago=1),
             "created_at": _iso(hours_ago=1)},
            {"id": "m2", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_stable_key": "k2",
             "source_timestamp": _iso(hours_ago=2),
             "created_at": _iso(hours_ago=2)},
        ]
        client = _fake_client_with_table({"extracted_memories": rows})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = recurring_patterns(
                workspace_id=TEST_WS, days=7, min_mentions=3,
            )
        assert out == []   # 2 < 3


# ====================================================================== #
# E. Proactive insights
# ====================================================================== #
class TestProactiveInsights:
    def test_stale_action_items_surfaced(self):
        """Action item from 20 days ago with no decision follow-up."""
        from analytics_intelligence import proactive_insights
        rows = [
            {"id": "a1", "kind": "action_item",
             "content": "deploy Friday", "owner": "Rahul",
             "entity_type": None,
             "source_kind": "slack",
             "source_stable_key": "slack:msg:1",
             "source_timestamp": _iso(days_ago=20),
             "created_at": _iso(days_ago=20)},
        ]
        client = _fake_client_with_table({"extracted_memories": rows})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = proactive_insights(
                workspace_id=TEST_WS, stale_action_days=14,
            )
        assert len(out["stale_action_items"]) == 1
        assert out["stale_action_items"][0]["owner"] == "Rahul"

    def test_followed_up_action_items_excluded(self):
        """Same owner posted a decision after the action item -> not stale."""
        from analytics_intelligence import proactive_insights
        rows = [
            {"id": "a1", "kind": "action_item",
             "content": "deploy Friday", "owner": "Rahul",
             "entity_type": None,
             "source_kind": "slack",
             "source_stable_key": "slack:msg:1",
             "source_timestamp": _iso(days_ago=20),
             "created_at": _iso(days_ago=20)},
            {"id": "d1", "kind": "decision",
             "content": "deployed Friday successfully", "owner": "Rahul",
             "entity_type": None,
             "source_kind": "slack",
             "source_stable_key": "slack:msg:2",
             "source_timestamp": _iso(days_ago=10),
             "created_at": _iso(days_ago=10)},
        ]
        client = _fake_client_with_table({"extracted_memories": rows})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = proactive_insights(
                workspace_id=TEST_WS, stale_action_days=14,
            )
        assert out["stale_action_items"] == []

    def test_dormant_projects_surfaced(self):
        from analytics_intelligence import proactive_insights
        rows = [
            {"id": "p1", "kind": "entity", "content": "Apollo",
             "entity_type": "project",
             "source_kind": "slack",
             "source_stable_key": "slack:msg:1",
             "source_timestamp": _iso(days_ago=45),
             "created_at": _iso(days_ago=45)},
        ]
        client = _fake_client_with_table({"extracted_memories": rows})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = proactive_insights(
                workspace_id=TEST_WS, dormant_project_days=30,
            )
        assert len(out["dormant_projects"]) == 1
        assert out["dormant_projects"][0]["content"] == "Apollo"

    def test_surging_entities_detected_with_doubling(self):
        """Entity mentioned 3x in last 7 days, 1x in the prior 7 days."""
        from analytics_intelligence import proactive_insights
        rows = [
            # Recent (last 7d): 3 mentions
            {"id": "m1", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_stable_key": "k1",
             "source_timestamp": _iso(days_ago=1),
             "created_at": _iso(days_ago=1)},
            {"id": "m2", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_stable_key": "k2",
             "source_timestamp": _iso(days_ago=3),
             "created_at": _iso(days_ago=3)},
            {"id": "m3", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_stable_key": "k3",
             "source_timestamp": _iso(days_ago=5),
             "created_at": _iso(days_ago=5)},
            # Prior 7-14d: 1 mention
            {"id": "m4", "kind": "entity", "content": "Kafka",
             "entity_type": "service",
             "source_stable_key": "k4",
             "source_timestamp": _iso(days_ago=10),
             "created_at": _iso(days_ago=10)},
        ]
        client = _fake_client_with_table({"extracted_memories": rows})
        with patch(
            "analytics_intelligence.get_supabase", return_value=client,
        ):
            out = proactive_insights(workspace_id=TEST_WS, surge_window_days=7)
        assert len(out["surging_entities"]) == 1
        s = out["surging_entities"][0]
        assert s["recent_mentions"] == 3
        assert s["prior_mentions"] == 1


# ====================================================================== #
# F. Routes integration
# ====================================================================== #
class TestAnalyticsRoutes:
    def test_overview_route(self, client, jwt_auth_headers):
        with patch(
            "main.aggregate_query_stats", create=True,
            return_value={"count": 5},
        ), patch(
            "main.aggregate_ingest_stats", create=True,
            return_value={"runs": 2},
        ), patch(
            "main.aggregate_retrieval_failure_stats", create=True,
            return_value={"count": 0, "recent": []},
        ):
            # The route does local imports, so patch the source module
            # too (the local-import lookup happens at request time).
            with patch(
                "analytics_store.aggregate_query_stats",
                return_value={"count": 5},
            ), patch(
                "analytics_store.aggregate_ingest_stats",
                return_value={"runs": 2},
            ), patch(
                "analytics_store.aggregate_retrieval_failure_stats",
                return_value={"count": 0, "recent": []},
            ):
                r = client.get(
                    "/api/analytics/overview?days=14",
                    headers=jwt_auth_headers,
                )
        assert r.status_code == 200
        body = r.json()
        assert body["window_days"] == 14
        assert body["query"] == {"count": 5}
        assert body["ingest"] == {"runs": 2}

    def test_overview_clamps_days(self, client, jwt_auth_headers):
        """Sending days=999 must clamp to 90 (the max_days cap)."""
        with patch(
            "analytics_store.aggregate_query_stats",
            return_value={"count": 0},
        ) as mock_q, patch(
            "analytics_store.aggregate_ingest_stats",
            return_value={"runs": 0},
        ), patch(
            "analytics_store.aggregate_retrieval_failure_stats",
            return_value={"count": 0, "recent": []},
        ):
            r = client.get(
                "/api/analytics/overview?days=999",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        # The query helper was called with days=90 (clamped).
        assert mock_q.call_args.kwargs["days"] == 90

    def test_topics_route(self, client, jwt_auth_headers):
        with patch(
            "analytics_intelligence.topic_overview",
            return_value={"top_entities": [], "cluster_count": 0},
        ):
            r = client.get(
                "/api/analytics/topics",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["top_entities"] == []

    def test_timeline_route(self, client, jwt_auth_headers):
        with patch(
            "analytics_intelligence.reconstruct_timeline",
            return_value=[{"id": "r1", "kind": "decision"}],
        ) as mock_t:
            r = client.get(
                "/api/analytics/timeline?entity=Kafka&kind=decision,action_item",
                headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        # Verify the route parsed the comma-separated kinds correctly.
        kwargs = mock_t.call_args.kwargs
        assert kwargs["entity"] == "Kafka"
        assert kwargs["kinds"] == ["decision", "action_item"]

    def test_insights_route(self, client, jwt_auth_headers):
        with patch(
            "analytics_intelligence.proactive_insights",
            return_value={
                "stale_action_items": [],
                "dormant_projects":   [],
                "surging_entities":   [],
            },
        ), patch(
            "analytics_intelligence.recurring_patterns",
            return_value=[],
        ):
            r = client.get(
                "/api/analytics/insights", headers=jwt_auth_headers,
            )
        assert r.status_code == 200
        body = r.json()
        for k in ("stale_action_items", "dormant_projects",
                  "surging_entities", "recurring"):
            assert k in body

    def test_routes_require_auth(self, client):
        for path in (
            "/api/analytics/overview",
            "/api/analytics/topics",
            "/api/analytics/timeline",
            "/api/analytics/insights",
        ):
            r = client.get(path)
            assert r.status_code == 401, f"{path} should require auth"


# ====================================================================== #
# G. Workspace isolation
# ====================================================================== #
class TestWorkspaceIsolation:
    """Every analytics function MUST filter by workspace_id. We can't
    introspect the actual SQL with the chainable mock without more
    plumbing, but we CAN assert that:
      - blank workspace_id short-circuits
      - the supabase call IS made (i.e. we don't skip workspace filtering)
    """
    def test_blank_workspace_short_circuits_topic_overview(self):
        from analytics_intelligence import topic_overview
        with patch("analytics_intelligence.get_supabase") as mock_get:
            out = topic_overview(workspace_id="", days=30)
        assert out == {"top_entities": [], "cluster_count": 0}
        mock_get.assert_not_called()

    def test_blank_workspace_short_circuits_timeline(self):
        from analytics_intelligence import reconstruct_timeline
        with patch("analytics_intelligence.get_supabase") as mock_get:
            out = reconstruct_timeline(workspace_id="", days=30)
        assert out == []
        mock_get.assert_not_called()

    def test_blank_workspace_short_circuits_emit(self):
        from analytics_store import emit_event
        with patch("analytics_store.get_supabase") as mock_get:
            ok = emit_event(workspace_id="", kind="query_completed")
        assert ok is False
        mock_get.assert_not_called()


# ====================================================================== #
# H. End-to-end emit on answer_question
# ====================================================================== #
class TestEmitOnAnswer:
    def test_answer_question_emits_query_completed(self):
        """When answer_question is called with a workspace_id, it should
        emit an analytics event recording the result shape."""
        from recall import answer_question
        # Patch heavy deps so we don't need real HydraDB / LLM.
        with patch(
            "recall.prepare_recall_context",
            return_value={
                "ready": True,
                "context_text": "ctx",
                "sources": [
                    {"index": 1, "source": "x", "source_kind": "slack"},
                ],
                "chunks_count": 1,
                "filtered_out": 0,
                "exact_matches": 0,
                "retrieval_mode": "default",
                "query_terms": [],
                "fallback_debug": None,
            },
        ), patch(
            "recall.generate_grounded_answer",
            return_value="raw answer text",
        ), patch(
            "recall.finalize_answer",
            return_value={
                "answer": "final",
                "cleaned_sources": [{"index": 1, "source_kind": "slack"}],
                "sources_before": 1, "sources_after": 1,
            },
        ), patch("analytics_store.emit_event") as mock_emit:
            answer_question(
                question="hello",
                workspace_id=TEST_WS,
                mode="default",
            )
        # An event was emitted on the success path.
        assert mock_emit.called
        kw = mock_emit.call_args.kwargs
        assert kw["workspace_id"] == TEST_WS
        assert kw["kind"] == "query_completed"
        payload = kw["payload"]
        # sources_count + source_kinds populated from the result.
        assert payload["sources_count"] == 1
        assert "slack" in payload["source_kinds"]

    def test_answer_question_emits_retrieval_failure_on_no_chunks(self):
        from recall import answer_question
        with patch(
            "recall.prepare_recall_context",
            return_value={
                "ready": False,
                "fallback_debug": {"reason": "no_chunks"},
            },
        ), patch("analytics_store.emit_event") as mock_emit:
            answer_question(
                question="hello",
                workspace_id=TEST_WS,
            )
        # At least one call was a retrieval_failure.
        kinds_emitted = [
            c.kwargs.get("kind") for c in mock_emit.call_args_list
        ]
        assert "retrieval_failure" in kinds_emitted

    def test_no_workspace_id_skips_emit(self):
        """Pre-Phase-12 callers that don't pass workspace_id should
        get no analytics emit -- the operation still works."""
        from recall import answer_question
        with patch(
            "recall.prepare_recall_context",
            return_value={
                "ready": True,
                "context_text": "ctx", "sources": [],
                "chunks_count": 0, "filtered_out": 0,
                "exact_matches": 0, "retrieval_mode": "default",
                "query_terms": [], "fallback_debug": None,
            },
        ), patch(
            "recall.generate_grounded_answer", return_value="a",
        ), patch(
            "recall.finalize_answer",
            return_value={
                "answer": "a", "cleaned_sources": [],
                "sources_before": 0, "sources_after": 0,
            },
        ), patch("analytics_store.emit_event") as mock_emit:
            answer_question(question="hello")    # no workspace_id
        mock_emit.assert_not_called()