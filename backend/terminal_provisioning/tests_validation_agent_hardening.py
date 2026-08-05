"""WS-J — validation-agent production-hardening DESIGN tests.

Validate the machine-readable design artefacts (health-model / monitoring-catalogue / runbook-index /
readiness-review), the runbooks.md structure, and the executable spec (validation_agent_spec). These are
DESIGN/repository tests only — no host, no MetaTrader5, no live validation, no Windows infrastructure."""
import json
import re
from pathlib import Path

from django.test import SimpleTestCase

from terminal_provisioning import validation_agent_spec as spec

_DOCS = Path(__file__).resolve().parents[2] / "docs" / "operations" / "validation-agent"


def _load(name):
    with open(_DOCS / name, encoding="utf-8") as fh:
        return json.load(fh)


class HealthModelArtefactTests(SimpleTestCase):
    def setUp(self):
        self.m = _load("health-model.json")
        self.names = [s["name"] for s in self.m["states"]]

    def test_states_match_the_spec_module(self):
        self.assertEqual(set(self.names), set(spec.STATES))
        self.assertEqual(len(self.names), len(set(self.names)))          # no duplicates

    def test_every_state_is_fully_specified(self):
        for s in self.m["states"]:
            for k in ("entry", "exit", "operator_action", "transitions"):
                self.assertTrue(s.get(k) not in (None, "", []), f"{s['name']} missing {k}")

    def test_transitions_reference_defined_states(self):
        for s in self.m["states"]:
            self.assertGreaterEqual(len(s["transitions"]), 1, f"{s['name']} has no transition")
            for t in s["transitions"]:
                self.assertIn(t["to"], self.names, f"{s['name']} -> undefined {t['to']}")
                self.assertTrue(t.get("when"), f"{s['name']} transition without a 'when'")

    def test_probe_chain_is_the_documented_ladder(self):
        layers = [p["layer"] for p in self.m["probe_chain"]]
        for req in ("process_running", "socket_listening", "negotiate_ok", "mt5_initialise",
                    "broker_login", "response_returned"):
            self.assertIn(req, layers)


class DeriveStateTests(SimpleTestCase):
    def test_ready_and_calm_is_healthy(self):
        self.assertEqual(spec.derive_agent_state(process_running=True, socket_listening=True,
                                                 negotiate_ok=True), "HEALTHY")

    def test_readiness_failures_are_unavailable(self):
        self.assertEqual(spec.derive_agent_state(process_running=True, socket_listening=False,
                                                 negotiate_ok=False), "UNAVAILABLE")
        self.assertEqual(spec.derive_agent_state(process_running=True, socket_listening=True,
                                                 negotiate_ok=False), "UNAVAILABLE")
        self.assertEqual(spec.derive_agent_state(process_running=False, socket_listening=False,
                                                 negotiate_ok=False), "UNAVAILABLE")

    def test_starting_and_recovery_hints_when_not_ready(self):
        self.assertEqual(spec.derive_agent_state(process_running=True, socket_listening=False,
                                                 negotiate_ok=False, lifecycle="starting"), "STARTING")
        self.assertEqual(spec.derive_agent_state(process_running=False, socket_listening=False,
                                                 negotiate_ok=False, lifecycle="recovery"), "RECOVERY")

    def test_ready_resolves_start_recovery_to_healthy(self):
        self.assertEqual(spec.derive_agent_state(process_running=True, socket_listening=True,
                                                 negotiate_ok=True, lifecycle="recovery"), "HEALTHY")

    def test_stopping_wins_over_readiness(self):
        self.assertEqual(spec.derive_agent_state(process_running=True, socket_listening=True,
                                                 negotiate_ok=True, lifecycle="stopping"), "STOPPING")

    def test_degraded_threshold_boundary_and_never_unavailable(self):
        # invariant: a downstream failure yields DEGRADED (agent up), NEVER UNAVAILABLE
        self.assertEqual(spec.derive_agent_state(process_running=True, socket_listening=True, negotiate_ok=True,
                                                 downstream_failure_rate=0.49, degraded_threshold=0.5), "HEALTHY")
        st = spec.derive_agent_state(process_running=True, socket_listening=True, negotiate_ok=True,
                                     downstream_failure_rate=1.0, degraded_threshold=0.5)
        self.assertEqual(st, "DEGRADED")
        self.assertNotEqual(st, "UNAVAILABLE")

    def test_invalid_lifecycle_raises(self):
        with self.assertRaises(ValueError):
            spec.derive_agent_state(process_running=True, socket_listening=True, negotiate_ok=True,
                                    lifecycle="bogus")


class MonitoringCalcTests(SimpleTestCase):
    def test_window_failure_rate(self):
        self.assertEqual(spec.window_failure_rate([]), 0.0)
        self.assertEqual(spec.window_failure_rate(["demo_ok", "demo_ok"]), 0.0)
        self.assertEqual(spec.window_failure_rate(
            ["demo_ok", "login_timeout", "validation_agent_unreachable", "demo_ok"]), 0.5)

    def test_uptime_ratio(self):
        self.assertEqual(spec.uptime_ratio([]), 0.0)
        self.assertEqual(spec.uptime_ratio([1, 1, 0, 1]), 0.75)

    def test_connect_timeout_signature(self):
        self.assertTrue(spec.is_connect_timeout_signature(10025, connect_timeout_ms=10000))   # the #13 case
        self.assertFalse(spec.is_connect_timeout_signature(120000, connect_timeout_ms=10000))  # MT5 login window
        self.assertFalse(spec.is_connect_timeout_signature(None))

    def test_dominant_latency_segment(self):
        self.assertEqual(spec.dominant_latency_segment({}), (None, 0.0))
        self.assertEqual(spec.dominant_latency_segment(
            {"agent_transport": 10000, "mt5_initialise": None, "broker_login": 200}),
            ("agent_transport", 10000.0))


class MonitoringCatalogueTests(SimpleTestCase):
    def setUp(self):
        self.hm = _load("health-model.json")
        self.mon = _load("monitoring-catalogue.json")
        self.rb = _load("runbook-index.json")

    def test_every_probe_layer_has_a_metric(self):
        metric_layers = {m["layer"] for m in self.mon["metrics"]}
        for p in self.hm["probe_chain"]:
            self.assertIn(p["layer"], metric_layers, f"no metric for health layer {p['layer']}")

    def test_metric_alerts_are_defined(self):
        alert_names = {a["name"] for a in self.mon["alerts"]}
        for m in self.mon["metrics"]:
            if m.get("alert"):
                self.assertIn(m["alert"], alert_names, f"metric {m['name']} -> unknown alert {m['alert']}")

    def test_every_alert_routes_to_a_real_runbook_and_valid_state(self):
        rb_ids = {r["id"] for r in self.rb["runbooks"]}
        for a in self.mon["alerts"]:
            self.assertIn(a["runbook"], rb_ids, f"alert {a['name']} -> unknown runbook {a['runbook']}")
            self.assertIn(a["severity"], ("CRITICAL", "HIGH", "MEDIUM", "LOW"))
            self.assertIn(a.get("detects_state"), list(spec.STATES) + [None])

    def test_latency_breakdown_covers_the_layers(self):
        segs = {s["segment"] for s in self.mon["latency_breakdown"]}
        for req in ("agent_transport", "mt5_initialise", "broker_login", "total"):
            self.assertIn(req, segs)

    def test_adversarial_gaps_are_now_monitored(self):
        # Folded-in adversarial findings must each have a metric + alert.
        metric_names = {m["name"] for m in self.mon["metrics"]}
        alert_names = {a["name"] for a in self.mon["alerts"]}
        for m in ("agent_supervised", "agent_readiness_freshness_seconds", "oldest_inflight_validation_seconds"):
            self.assertIn(m, metric_names, f"missing adversarial-fold metric {m}")
        for a in ("agent_unsupervised_listener", "readiness_probe_stale", "validation_wedged"):
            self.assertIn(a, alert_names, f"missing adversarial-fold alert {a}")

    def test_key_downstream_reasons_are_monitored(self):
        # every reason flagged by the review must be monitored by some metric (matched in its name or source)
        haystack = " ".join(m.get("name", "") + " " + m.get("source", "") for m in self.mon["metrics"])
        for reason in ("validation_ipc_unavailable", "validation_busy", "server_unavailable",
                       "login_timeout", "validation_agent_unreachable"):
            self.assertIn(reason, haystack, f"downstream reason {reason} has no monitoring metric")


class RunbookTests(SimpleTestCase):
    def setUp(self):
        self.rb = _load("runbook-index.json")
        self.md = (_DOCS / "runbooks.md").read_text(encoding="utf-8")

    def test_index_contract(self):
        self.assertEqual(self.rb["first_section_must_be"], "Evidence")
        self.assertEqual(self.rb["required_sections"][0], "Evidence")
        self.assertEqual(self.rb["required_sections"][-1], "Escalation")

    def test_every_runbook_present_with_required_sections_evidence_first(self):
        blocks = re.split(r"^### ", self.md, flags=re.M)[1:]
        by_id = {}
        for b in blocks:
            m = re.search(r"\(([a-z0-9-]+)\)", b.splitlines()[0])
            if m:
                by_id[m.group(1)] = b
        for r in self.rb["runbooks"]:
            self.assertIn(r["id"], by_id, f"runbook {r['id']} missing from runbooks.md")
            headings = re.findall(r"^#### (\w+)", by_id[r["id"]], flags=re.M)
            for sec in self.rb["required_sections"]:
                self.assertIn(sec, headings, f"{r['id']} missing '{sec}' section")
            self.assertEqual(headings[0], "Evidence", f"{r['id']} first section must be Evidence")

    def test_runbook_detects_states_are_valid(self):
        for r in self.rb["runbooks"]:
            self.assertIn(r.get("detects_state"), list(spec.STATES) + [None])


class ReadinessReviewTests(SimpleTestCase):
    def test_items_well_formed_and_beta_minimum_marked(self):
        rr = _load("readiness-review.json")
        self.assertTrue(rr["items"])
        for it in rr["items"]:
            self.assertIn(it["severity"], ("Critical", "High", "Medium", "Low"))
            for k in ("id", "title", "evidence", "recommendation"):
                self.assertTrue(it.get(k), f"item {it.get('id')} missing {k}")
        self.assertTrue(any(it.get("minimum_for_beta") for it in rr["items"]),
                        "at least one minimum-for-beta item must be marked")
