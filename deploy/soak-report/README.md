# WS-G — Soak-report evidence collection

`python manage.py soak_report` captures a durable `SoakSnapshot` (by-source production metrics over
a window) and prints a summary. Runs hourly via `crontab.soak` (install with `install_soak_cron.sh`),
so the soak evidence accrues VPS-side with no developer/Claude process running.

- Baseline: run once (`soak_report --no-persist` for a dry-run, or persist the first row).
- Meaningful full-soak result: after ≥24–72h of continuous operation once the strategies are armed
  (and, if provider commands are armed, after real follow-up traffic). Query trends:
  `SoakSnapshot.objects.order_by('-generated_at')`.
