"""
GFX-PKT-WAYOND-PARSER-CERTIFICATION — certification report command (repo-only).

Replays the Wayond corpus through the real parser + dispatcher content precedence
and prints a certification report. Exits non-zero if any message is UNSAFE or FAILs,
so it can serve as a regression gate. No Telegram, no DB writes, no order.

    python manage.py certify_wayond
    python manage.py certify_wayond --corpus /path/to/other_corpus.json
"""

from django.core.management.base import BaseCommand

from signal_intake.certification import build_report, load_corpus


class Command(BaseCommand):
    help = "Certify the Wayond parser against the real-message corpus (repo-only)."

    def add_arguments(self, parser):
        parser.add_argument("--corpus", default=None,
                            help="Path to a corpus JSON (defaults to wayond_corpus.json).")

    def handle(self, *args, **o):
        entries = load_corpus(o["corpus"])
        report = build_report(entries)
        s = report["summary"]

        self.stdout.write(self.style.MIGRATE_HEADING("WAYOND PARSER CERTIFICATION"))
        self.stdout.write(f"corpus: {s['total']} real message(s)\n")
        self.stdout.write(f"  {'id':<34} {'expected':<12} {'observed':<12} {'verdict':<9} safety")
        self.stdout.write("  " + "-" * 78)
        for r in report["results"]:
            line = f"  {r['id']:<34} {r['expected']:<12} {r['observed']:<12} {r['verdict']:<9} {r['safety']}"
            style = self.style.SUCCESS if r["safety"] == "SAFE" and r["verdict"] != "FAIL" \
                else (self.style.ERROR if r["safety"] == "UNSAFE" else self.style.WARNING)
            self.stdout.write(style(line))

        self.stdout.write("")
        self.stdout.write("Classification summary (observed): " + (
            ", ".join(f"{k}={v}" for k, v in sorted(s["by_observed"].items())) or "none"))
        v = s["verdicts"]
        self.stdout.write(f"Verdicts: PASS={v['PASS']} DEGRADED={v['DEGRADED']} FAIL={v['FAIL']}")
        self.stdout.write("Unsafe: " + (", ".join(s["unsafe"]) or "none"))
        self.stdout.write("Degraded (safe, parser could improve): " + (", ".join(s["degraded"]) or "none"))

        if s["certified"]:
            self.stdout.write(self.style.SUCCESS(
                f"\nRESULT: CERTIFIED — {s['total']} message(s), 0 unsafe, 0 fail."))
        else:
            self.stdout.write(self.style.ERROR(
                f"\nRESULT: NOT CERTIFIED — {len(s['unsafe'])} unsafe, {v['FAIL']} fail."))
            raise SystemExit(1)
