"""A slow workload must not be allowed to consume the run.

FalkorDB's three-hop neighbourhood scan does not return promptly under a 1 vCPU
cap. With no enforced bound, a smoke run - one iteration per workload, meant to
prove the plumbing in minutes - took hours. `query_timeout_s` had been in the
config the whole time and was wired to nothing.

Two properties matter and are tested separately:

  * a bound is actually enforced, whatever the engine does about it;
  * exceeding it produces a clean TIMEOUT result and the suite carries on,
    rather than an exception, a hang, or a fabricated latency.
"""

from __future__ import annotations

import time

import pytest
from test_pipeline import AdapterFactory, MemoryAdapter, make_config

from benchmark.core.errors import QueryTimeout
from benchmark.core.results import Iteration
from benchmark.databases import registry
from benchmark.runners.deadline import Deadline
from benchmark.runners.runner import TIMEOUT_MARKER, _timed_out, _timeout_for, run_benchmark


class TestDeadline:
    def test_disabled_when_no_timeout_is_set(self):
        with Deadline(None) as deadline:
            assert not deadline.enabled
            assert deadline.call(lambda: 42) == 42

    def test_disabled_for_a_non_positive_timeout(self):
        # 0 is the documented way to ask for no ceiling at all.
        with Deadline(0) as deadline:
            assert not deadline.enabled

    def test_fast_call_returns_normally(self):
        with Deadline(5.0, grace=0.0) as deadline:
            assert deadline.call(lambda: "done") == "done"
            assert not deadline.expired

    def test_slow_call_raises_query_timeout(self):
        with Deadline(0.2, grace=0.0) as deadline:
            with pytest.raises(QueryTimeout):
                deadline.call(time.sleep, 5)
            assert deadline.expired

    def test_timeout_is_bounded_in_wall_clock(self):
        started = time.monotonic()
        with Deadline(0.2, grace=0.1) as deadline, pytest.raises(QueryTimeout):
            deadline.call(time.sleep, 30)
        # The whole point: we stop waiting. Closing must not join the abandoned
        # worker, or this would take the full 30 seconds.
        assert time.monotonic() - started < 5

    def test_grace_lets_a_native_timeout_win_first(self):
        # An engine honouring its own bound should get to return its own clean
        # error rather than being pre-empted by ours.
        with Deadline(1.0, grace=2.0) as deadline:
            assert deadline.call(lambda: "server answered") == "server answered"

    def test_exceptions_propagate_unchanged(self):
        def boom():
            raise ValueError("not a timeout")

        with Deadline(5.0) as deadline, pytest.raises(ValueError, match="not a timeout"):
            deadline.call(boom)

    def test_close_is_safe_twice(self):
        deadline = Deadline(1.0)
        deadline.close()
        deadline.close()


class TestTimeoutResolution:
    def test_falls_back_to_the_run_wide_bound(self):
        config = make_config(["point_lookup"])
        config.run.query_timeout_s = 90.0
        workload_config = config.workloads[0]
        assert _timeout_for(workload_config, config) == 90.0

    def test_per_workload_override_wins(self):
        # The workloads are not comparable in cost. One global number would
        # have to be set to the slowest, which stops bounding the fast ones.
        config = make_config(["point_lookup"], point_lookup={"timeout_s": 5})
        assert _timeout_for(config.workloads[0], config) == 5.0

    def test_zero_disables_the_bound(self):
        config = make_config(["point_lookup"], point_lookup={"timeout_s": 0})
        assert _timeout_for(config.workloads[0], config) == 0.0


class TestTimeoutMarking:
    def test_timed_out_iterations_are_recognisable(self):
        assert _timed_out(Iteration(0, 0, 0, ok=False, error=f"{TIMEOUT_MARKER}too slow"))

    def test_ordinary_failures_are_not_timeouts(self):
        # A rejected query is a bug in the harness; a timeout is a property of
        # the engine at this cap. Conflating them sends people to fix the wrong
        # thing.
        assert not _timed_out(Iteration(0, 0, 0, ok=False, error="SyntaxError: bad query"))

    def test_successful_iterations_are_not_timeouts(self):
        assert not _timed_out(Iteration(0, 1000, 5))


class TestRunnerBoundsTheRun:
    def _factory(self, monkeypatch, configure):
        built = AdapterFactory(configure)
        monkeypatch.setitem(registry.ADAPTERS, "memory", MemoryAdapter)
        monkeypatch.setattr("benchmark.runners.runner.build_adapter", built)
        return built

    def test_the_configured_bound_reaches_the_adapter(self, toy_graph, monkeypatch):
        built = self._factory(monkeypatch, None)
        config = make_config(["point_lookup"], point_lookup={"timeout_s": 7})
        run_benchmark(config, toy_graph)

        seen = built.stores["engine-a"].timeouts_seen
        # Proving the wiring, not the behaviour: query_timeout_s sat in the
        # config, referenced by nothing, for the whole life of the project.
        assert seen, "the adapter was never told about a timeout"
        assert all(value == 7.0 for value in seen), seen

    def test_no_bound_is_passed_when_timeouts_are_disabled(self, toy_graph, monkeypatch):
        built = self._factory(monkeypatch, None)
        config = make_config(["point_lookup"], point_lookup={"timeout_s": 0})
        run_benchmark(config, toy_graph)
        assert all(value in (None, 0.0) for value in built.stores["engine-a"].timeouts_seen)

    def test_a_hanging_workload_is_abandoned_and_the_suite_continues(self, toy_graph, monkeypatch):
        def configure(name, store):
            if name == "engine-b":
                store.hang_on = {"one_hop": 30.0}

        self._factory(monkeypatch, configure)
        config = make_config(
            ["point_lookup", "one_hop", "top_cited"],
            one_hop={"timeout_s": 0.3},
        )

        started = time.monotonic()
        results = run_benchmark(config, toy_graph)
        elapsed = time.monotonic() - started

        # Bounded: without this the run would take 30s per iteration.
        assert elapsed < 25, f"run took {elapsed:.1f}s; the bound was not enforced"

        slow = results.find("engine-b", "one_hop")
        assert slow.status == "timeout"
        assert "did not complete within" in slow.note

        # And the suite carried on to the workload after it.
        after = results.find("engine-b", "top_cited")
        assert after is not None and after.status == "ok"

        # The healthy engine is unaffected.
        assert results.find("engine-a", "one_hop").status == "ok"

    def test_a_timeout_produces_no_latency_measurement(self, toy_graph, monkeypatch):
        def configure(name, store):
            store.hang_on = {"one_hop": 30.0}

        self._factory(monkeypatch, configure)
        config = make_config(["one_hop"], one_hop={"timeout_s": 0.3})
        results = run_benchmark(config, toy_graph)

        run = results.find("engine-a", "one_hop")
        # The abandoned call has a duration, but it is not a measurement of
        # anything and must never reach the statistics.
        assert run.measured_ns() == []
        assert run.status == "timeout"

    def test_abandons_after_the_first_timeout_rather_than_every_iteration(
        self, toy_graph, monkeypatch
    ):
        def configure(name, store):
            store.hang_on = {"one_hop": 10.0}

        built = self._factory(monkeypatch, configure)
        config = make_config(["one_hop"], one_hop={"timeout_s": 0.3})
        config.run.measured_iterations = 50
        run_benchmark(config, toy_graph)

        store = built.stores["engine-a"]
        # 50 iterations x 10s would be over eight minutes. One is enough to
        # establish that this engine exceeds the bound.
        hung = [t for t in store.timeouts_seen]
        assert len(hung) <= 3, f"kept retrying a known-slow query {len(hung)} times"
