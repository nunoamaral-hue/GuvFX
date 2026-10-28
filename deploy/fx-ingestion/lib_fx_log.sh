#!/usr/bin/env bash
# lib_fx_log.sh - shared FX log-file provisioning + cron-user redirection-boundary helpers.
#
# WHY THIS EXISTS: the FX cron lines run as the unprivileged `ubuntu` user and redirect their output with
#   >> /var/log/guvfx/fetch_fx_boe.log   (and  >> /var/log/guvfx/fx_freshness.log)
# /var/log/guvfx is root-owned 0755, so `ubuntu` can APPEND to a file it already owns there but CANNOT CREATE a
# new one. When those log files did not exist, /bin/sh could not open the redirect target, so the scheduled
# command failed to start and (with MAILTO unset) failed silently. Checking only that the directory exists does
# NOT catch this - the real failure is at the cron user's "create/append the redirect target" boundary.
#
# These helpers are sourced by install_fx_ingestion_cron.sh and verify_fx_ingestion.sh, and exercised directly by
# tests/test_log_boundary.sh, so the installer, the verifier and the regression test all share one definition of
# the boundary. ASCII-only; no secret is ever written here (the helpers create empty files / open for append).

# Least-privilege mode for operational logs: owner rw, group r, no world access. Overridable for tests.
FX_LOG_MODE="${FX_LOG_MODE:-0640}"

# fx_ensure_log <path>
# Idempotently ensure <path> exists, owned by the invoking (cron) user, at the least-privilege mode. Uses a
# non-sudo create first (works when the directory is user-writable, e.g. tests); falls back to sudo only when the
# directory is root-owned (production). Never touches a file that already exists, and never widens the directory.
fx_ensure_log() {
  local f="$1" owner group
  owner="$(id -un)"; group="$(id -gn)"
  if [ ! -e "$f" ]; then
    install -m "$FX_LOG_MODE" -o "$owner" -g "$group" /dev/null "$f" 2>/dev/null \
      || sudo install -m "$FX_LOG_MODE" -o "$owner" -g "$group" /dev/null "$f"
  fi
}

# fx_check_appendable <path>
# Return 0 iff the current user can open <path> for append via the exact '>>' redirection path cron uses, and
# non-zero otherwise. Writes no bytes (`:` produces no output), so it never pollutes the log or leaks a secret.
fx_check_appendable() {
  local f="$1"
  ( : >> "$f" ) 2>/dev/null
}
