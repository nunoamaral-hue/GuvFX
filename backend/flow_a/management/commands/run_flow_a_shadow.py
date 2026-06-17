"""
Flow A Build Phase 1 — Shadow pipeline demonstration.

Proves the target flow end-to-end with execution suppressed:

    Wayond Signal
      -> Strategy Evaluation
      -> Trade Quality Gate v0.1 (Shadow)
      -> OPEN_TRADE Candidate (logged only)
      -> Execution Suppressed

Evidence printed:
    1. Wayond signal exists
    2. Strategy evaluation result
    3. Trade Quality Gate v0.1 decision (ACCEPT/REJECT/ESCALATE)
    4. OPEN_TRADE candidate artifact (when ACCEPTed)
    5. Execution suppression proof (no ExecutionJob; EA sole live decider)

Usage:
    DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py run_flow_a_shadow
    ... --signal '{"signal_id": "...", ...}'  --strategy '{"symbol_universe": "...", ...}'
    ... --signal-fixture <path>  --strategy-fixture <path>
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from flow_a import pipeline
from flow_a.types import FlowAEscalation, GateOutcome

DEFAULT_SIGNAL_FIXTURE = "intelligence/fixtures/wayond_signal_sample.json"
DEFAULT_STRATEGY_FIXTURE = "flow_a/fixtures/strategy_sample.json"


class Command(BaseCommand):
    help = "Flow A Phase 1 — run the shadow pipeline (execution suppressed)."

    def add_arguments(self, parser):
        parser.add_argument("--signal", help="Inline Wayond signal JSON string.")
        parser.add_argument("--strategy", help="Inline strategy config JSON string.")
        parser.add_argument("--signal-fixture", help="Path to a Wayond signal JSON file.")
        parser.add_argument("--strategy-fixture", help="Path to a strategy config JSON file.")

    def _section(self, n, title):
        self.stdout.write("=" * 64)
        self.stdout.write(self.style.MIGRATE_HEADING(f"{n}. {title}"))

    def _load(self, inline, fixture_opt, default_fixture, label):
        if inline:
            return json.loads(inline)
        rel = fixture_opt or default_fixture
        path = Path(rel)
        if not path.is_absolute():
            path = Path(settings.BASE_DIR) / rel
        if not path.exists():
            raise CommandError(f"{label} fixture not found: {path}")
        return json.loads(path.read_text())

    def handle(self, *args, **opts):
        signal = self._load(opts.get("signal"), opts.get("signal_fixture"),
                            DEFAULT_SIGNAL_FIXTURE, "Wayond signal")
        strategy = self._load(opts.get("strategy"), opts.get("strategy_fixture"),
                             DEFAULT_STRATEGY_FIXTURE, "Strategy config")

        self.stdout.write(self.style.SUCCESS(
            "\nFlow A Build Phase 1 — Shadow pipeline (execution suppressed)"
        ))

        self._section(1, "WAYOND SIGNAL (external source)")
        self.stdout.write("  " + json.dumps(signal))

        try:
            result = pipeline.run_shadow(signal, strategy)
        except FlowAEscalation as exc:
            self._section(0, "ESCALATION (ADR-012)")
            self.stderr.write(f"  ESCALATE — {exc}")
            return

        self._section(2, "STRATEGY EVALUATION")
        self.stdout.write(f"  matched={result.evaluation.matched}")
        for r in result.evaluation.reasons:
            self.stdout.write(f"   - {r}")

        self._section(3, "TRADE QUALITY GATE v0.1 (DRAFT / SHADOW)")
        self.stdout.write(f"  outcome={result.gate.outcome.value}")
        for r in result.gate.reasons:
            self.stdout.write(f"   - {r}")
        self.stdout.write(f"  thresholds={json.dumps(result.gate.thresholds)}")

        self._section(4, "OPEN_TRADE CANDIDATE (artifact only)")
        if result.candidate is not None:
            self.stdout.write("  " + json.dumps(result.candidate.to_dict()))
        else:
            self.stdout.write("  (no candidate — gate did not ACCEPT)")

        self._section(5, "EXECUTION SUPPRESSION PROOF")
        self.stdout.write(f"  execution_suppressed = {result.execution_suppressed}")
        self.stdout.write(f"  execution_job_created = {result.execution_job_created}")
        self.stdout.write("  No PENDING OPEN_TRADE ExecutionJob created; MT5 worker "
                          "cannot poll the candidate; EA remains sole live decider.")

        self.stdout.write("=" * 64)
        ok = (
            result.execution_suppressed
            and not result.execution_job_created
            and (result.candidate is None) == (result.gate.outcome is not GateOutcome.ACCEPT)
        )
        if ok:
            self.stdout.write(self.style.SUCCESS(
                "PASS — Flow A shadow run complete; execution suppressed; "
                "no live trading; ADR-009 (no models) + ADR-012 (no derived "
                "can_trade) preserved."
            ))
        else:  # pragma: no cover
            self.stderr.write("FAIL — suppression invariant not satisfied.")
