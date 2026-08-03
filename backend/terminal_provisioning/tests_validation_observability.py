"""ADR-0027 observability enhancement (2026-08-03) — validation-runner diagnostics, deterministic terminal
termination, evidence-before-scrub ordering and secret safety.

Imports the deploy/beta-agent bundle (no Windows/MT5/network); the Windows adapter is faked so the runner's
capture → terminate → scrub → restore flow is exercised end-to-end off-host.
"""
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import validation_diagnostics as diag        # noqa: E402
import validation_handoff as handoff          # noqa: E402
import validation_runner as runner            # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────────────
class _FakeHandler:
    def __init__(self, outcome):
        self._o = outcome

    def validate(self, **_kw):
        return dict(self._o)


class _FakeWin:
    """Fake Windows adapter — records termination calls; returns configured processes / survivors."""
    def __init__(self, procs=None, remaining=None, session=7):
        self._procs = procs or []
        self._remaining = remaining or []
        self._session = session
        self.terminate_calls = []

    def find_terminal_processes(self, _vdir, _forbidden, image_name="terminal64.exe"):
        return [dict(p) for p in self._procs]

    def session_of(self, _pid):
        return self._session

    def terminate_terminal_processes(self, vdir, forbidden, image_name="terminal64.exe"):
        self.terminate_calls.append((vdir, tuple(forbidden)))
        pids = [p["pid"] for p in self._procs]
        return {"targets": pids, "killed": pids, "failed": [], "remaining": list(self._remaining)}


def _cfg(tmp, **over):
    c = {
        "validation_handoff_dir": os.path.join(tmp, "handoff"),
        "validation_diagnostics_dir": os.path.join(tmp, "diag"),
        "validation_terminal_dir": os.path.join(tmp, "term"),
        "validation_precompiled_dir": os.path.join(tmp, "precompiled"),
        "login_timeout_ms": 30000,
        "slots_root": r"C:\GuvFX\beta\slots",
        "golden_dir": r"C:\GuvFX\golden",
        "beta_root": r"C:\GuvFX\beta",
    }
    c.update(over)
    os.makedirs(c["validation_handoff_dir"], exist_ok=True)
    os.makedirs(os.path.join(c["validation_terminal_dir"], "config"), exist_ok=True)
    return c


def _stage_request(handoff_dir, *, login="1302575", correlation_id="corr-1"):
    rid = handoff.new_request_id()
    handoff.write_request(handoff_dir, rid, {
        "operation": "VALIDATE_LOGIN", "runtime_uuid": "u", "correlation_id": correlation_id,
        "nonce": "n", "payload": {"login": login, "server": "IS6Technologies-Demo",
                                  "password_env": {"ct": "x"}}}, ttl_seconds=120)
    return rid


def _read_result(handoff_dir, rid):
    key = handoff.local_key(handoff_dir)
    return handoff._read_tagged(os.path.join(handoff_dir, rid + handoff._RES), key)


def _read_evidence(diag_dir, correlation_id):
    cid = correlation_id.replace("/", "_")
    path = os.path.join(diag_dir, cid + ".diag.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ── journal decode + milestones ─────────────────────────────────────────────────────────────────────────
class JournalDecodeTests(unittest.TestCase):
    def test_utf8(self):
        self.assertIn("started", diag.decode_journal_bytes("MetaTrader 5 started".encode("utf-8")).lower())

    def test_utf16_le_bom(self):
        raw = "MetaTrader 5 build 6073 started".encode("utf-16-le")
        raw = b"\xff\xfe" + raw
        self.assertIn("started", diag.decode_journal_bytes(raw).lower())

    def test_utf16_be_bom(self):
        raw = b"\xfe\xff" + "authorized on Server".encode("utf-16-be")
        self.assertIn("authorized", diag.decode_journal_bytes(raw).lower())

    def test_utf16_le_no_bom_heuristic(self):
        # BOM-less UTF-16LE (the RULE-11 trap) — every other byte is NUL for ASCII text.
        raw = "connecting to 194.164.179.28:443".encode("utf-16-le")
        self.assertIn("connecting", diag.decode_journal_bytes(raw).lower())

    def test_empty(self):
        self.assertEqual(diag.decode_journal_bytes(b""), "")

    def test_milestones_allowlisted(self):
        text = ("MetaTrader 5 build 6073 started\n"
                "bind error on 127.0.0.1:22346\n"
                "connecting to 194.164.179.28:443\n"
                "authorized on IS6Technologies-Demo\n"
                "random unrelated line with no meaning\n")
        codes = diag.extract_milestones(text)
        self.assertIn("TERMINAL_STARTED", codes)
        self.assertIn("MCP_BIND_CONFLICT", codes)
        self.assertIn("BROKER_AUTHORISED", codes)
        for c in codes:
            self.assertIn(c, diag.MILESTONE_CODES)

    def test_mdi_failure_milestone(self):
        text = "Window MDI unhook/create failed\ncreate new frame CHART01.CHR failed"
        self.assertIn("GUI_MDI_CREATE_FAILED", diag.extract_milestones(text))

    def test_unknown_line_no_code(self):
        self.assertEqual(diag.extract_milestones("completely unrelated text\nanother"), [])

    def test_read_journal_milestones_picks_newest(self):
        with tempfile.TemporaryDirectory() as d:
            calls = {"old": b"old junk", "new": "authorized on X".encode("utf-16-le")}
            listing = [("20260101.log", 1.0), ("20260102.log", 2.0)]

            def _list(_d):
                return listing

            def _reader(path):
                return calls["new"] if path.endswith("20260102.log") else calls["old"]

            codes = diag.read_journal_milestones([d], list_dir=_list, reader=_reader)
            self.assertIn("BROKER_AUTHORISED", codes)

    def test_read_journal_no_files(self):
        self.assertEqual(diag.read_journal_milestones(["/nonexistent"], list_dir=lambda d: [],
                                                      reader=lambda p: b""), [])


# ── redaction / secret safety ───────────────────────────────────────────────────────────────────────────
class RedactionTests(unittest.TestCase):
    def test_looks_secret(self):
        self.assertTrue(diag.looks_secret("my password is hunter2"))
        self.assertTrue(diag.looks_secret("-----BEGIN PRIVATE KEY-----"))
        self.assertTrue(diag.looks_secret("api_key=abc"))
        self.assertFalse(diag.looks_secret("connecting to broker"))

    def test_scrub_drops_secret_and_bounds_length(self):
        self.assertEqual(diag.scrub("token=deadbeef"), "[REDACTED]")
        self.assertEqual(len(diag.scrub("x" * 5000)), 512)

    def test_scrub_nested(self):
        out = diag.scrub({"a": ["ok", "secret=1"], "b": {"c": "password: p"}})
        self.assertEqual(out["a"][1], "[REDACTED]")
        self.assertEqual(out["b"]["c"], "[REDACTED]")

    def test_build_evidence_allowlist_and_scrub(self):
        ev = diag.build_evidence({"correlation_id": "c", "unknown_key": "leak",
                                  "last_error_reason": "Authorization failed",
                                  "reason_code": "password=oops"})
        self.assertIn("correlation_id", ev)
        self.assertNotIn("unknown_key", ev)                 # unknown key dropped
        self.assertEqual(ev["reason_code"], "[REDACTED]")   # secret-looking value scrubbed
        self.assertEqual(ev["schema_version"], diag.SCHEMA_VERSION)

    def test_evidence_never_holds_password(self):
        ev = diag.build_evidence({"last_error_reason": "conn ok", "journal_milestones": ["TERMINAL_STARTED"]})
        blob = json.dumps(ev)
        for bad in ("password", "hunter2", "BEGIN PRIVATE", "ciphertext"):
            self.assertNotIn(bad, blob)


# ── stage localisation ──────────────────────────────────────────────────────────────────────────────────
class StageLocalisationTests(unittest.TestCase):
    def test_all_reached(self):
        last, first = diag.stage_localisation(diag.STAGES)
        self.assertEqual(first, None)
        self.assertEqual(last, "COMPLETE")

    def test_gap_reports_first_failing(self):
        reached = {"REQUEST_ACCEPTED", "RUNNER_STARTED", "ENVELOPE_OPENED", "TERMINAL_LAUNCHED",
                   "GUI_READY", "IPC_READY", "BROKER_TCP_CONNECTED", "SHUTDOWN_REQUESTED"}  # AUTH missing
        last, first = diag.stage_localisation(reached)
        self.assertEqual(first, "BROKER_AUTHORISED")
        self.assertEqual(last, "BROKER_TCP_CONNECTED")

    def test_later_stage_does_not_imply_earlier(self):
        # SHUTDOWN_REQUESTED reached but GUI missing → first failing is still GUI_READY.
        last, first = diag.stage_localisation({"REQUEST_ACCEPTED", "RUNNER_STARTED", "ENVELOPE_OPENED",
                                               "TERMINAL_LAUNCHED", "SHUTDOWN_REQUESTED"})
        self.assertEqual(first, "GUI_READY")
        self.assertEqual(last, "TERMINAL_LAUNCHED")

    def test_derive_stages_login_timeout_after_tcp(self):
        op = {"initialize_started": True, "initialize_result": False, "account_info_present": False}
        stages = runner._derive_stages(op, ["TERMINAL_STARTED", "BROKER_TCP_ESTABLISHED"])
        _last, first = diag.stage_localisation(stages)
        self.assertEqual(first, "BROKER_AUTHORISED")        # TCP reached, auth did not

    def test_derive_stages_success(self):
        op = {"initialize_started": True, "initialize_result": True, "account_info_present": True,
              "trade_mode": 0}
        stages = runner._derive_stages(op, ["TERMINAL_STARTED", "BROKER_AUTHORISED", "ACCOUNT_INFO_READY"])
        last, first = diag.stage_localisation(stages)
        self.assertEqual(last, "CLASSIFIED")                # login pipeline reached classification
        self.assertEqual(first, "SHUTDOWN_REQUESTED")       # next stage is cleanup (added by the runner)


# ── termination path guard ──────────────────────────────────────────────────────────────────────────────
class TerminationGuardTests(unittest.TestCase):
    TDIR = r"C:\GuvFX\beta\validation\terminal"
    FORBID = (r"C:\GuvFX\beta\slots", r"C:\GuvFX\golden", r"C:\GuvFX\beta\accounts")

    def test_validation_terminal_is_terminatable(self):
        self.assertTrue(diag.is_terminatable(self.TDIR + r"\terminal64.exe", self.TDIR, self.FORBID))

    def test_customer_zero_slot_rejected(self):
        self.assertFalse(diag.is_terminatable(r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe",
                                              self.TDIR, self.FORBID))

    def test_production_path_rejected(self):
        self.assertFalse(diag.is_terminatable(r"C:\Program Files\MetaTrader 5\terminal64.exe",
                                              self.TDIR, self.FORBID))

    def test_golden_rejected(self):
        self.assertFalse(diag.is_terminatable(r"C:\GuvFX\golden\newMT5\terminal64.exe",
                                              self.TDIR, self.FORBID))

    def test_traversal_rejected(self):
        self.assertFalse(diag.is_terminatable(self.TDIR + r"\..\slots\1\terminal64.exe",
                                              self.TDIR, self.FORBID))

    def test_relative_and_empty_rejected(self):
        self.assertFalse(diag.is_terminatable("terminal64.exe", self.TDIR, self.FORBID))
        self.assertFalse(diag.is_terminatable("", self.TDIR, self.FORBID))
        self.assertFalse(diag.is_terminatable(self.TDIR + r"\terminal64.exe", "", self.FORBID))

    def test_select_terminatable(self):
        procs = [(1, self.TDIR + r"\terminal64.exe"),
                 (2, r"C:\GuvFX\beta\slots\2\terminal\terminal64.exe"),
                 (3, self.TDIR + r"\sub\terminal64.exe")]
        self.assertEqual(sorted(diag.select_terminatable(procs, self.TDIR, self.FORBID)), [1, 3])


# ── durable artefact store ──────────────────────────────────────────────────────────────────────────────
class ArtefactStoreTests(unittest.TestCase):
    def test_write_and_sweep(self):
        with tempfile.TemporaryDirectory() as d:
            p = diag.write_evidence(d, "corr/1", {"correlation_id": "corr/1", "reason_code": "login_timeout"},
                                    now=1000.0)
            self.assertTrue(os.path.exists(p))
            loaded = json.load(open(p, encoding="utf-8"))
            self.assertEqual(loaded["correlation_id"], "corr/1")
            # sweep compares against the file's real mtime; anchor the logical clock on it.
            mtime = os.path.getmtime(p)
            self.assertEqual(diag.sweep_expired(d, max_age_s=100000, now=mtime + 10), 0)   # young → kept
            self.assertEqual(diag.sweep_expired(d, max_age_s=1, now=mtime + 100000), 1)    # old → removed
            self.assertFalse(os.path.exists(p))

    def test_correlation_id_sanitised_in_filename(self):
        with tempfile.TemporaryDirectory() as d:
            p = diag.write_evidence(d, "../../etc/passwd", {"correlation_id": "x"}, now=1.0)
            self.assertEqual(os.path.dirname(os.path.abspath(p)), os.path.abspath(d))  # no traversal escape

    def test_operator_summary_keys(self):
        s = diag.operator_summary({"correlation_id": "c", "stage_reached": "BROKER_TCP_CONNECTED",
                                   "first_failing_stage": "BROKER_AUTHORISED", "last_error_code": -10005,
                                   "last_error_reason": "timeout", "baseline_restore_result": "restored",
                                   "terminal_exited_after_shutdown": True})
        self.assertEqual(s["evidence_id"], "c")
        self.assertEqual(s["first_failing_stage"], "BROKER_AUTHORISED")
        self.assertEqual(s["terminal_exit_status"], True)


# ── runner integration (capture → terminate → scrub → restore) ──────────────────────────────────────────
class RunnerFlowTests(unittest.TestCase):
    def _run(self, tmp, outcome, *, win=None, milestones=None, mirror=None, diag_dir=True, **cfgover):
        cfg = _cfg(tmp, **cfgover)
        if not diag_dir:
            cfg["validation_diagnostics_dir"] = ""
        rid = _stage_request(cfg["validation_handoff_dir"])
        status = runner.run_once(
            cfg, build_handler=lambda _c: _FakeHandler(outcome), win=win or _FakeWin(),
            clock=lambda: 1000.0, read_journal=lambda dirs: list(milestones or []),
            mirror_baseline=(mirror or (lambda s, d: "restored")))
        return cfg, rid, status

    def test_login_timeout_capture_and_localisation(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = {"ok": False, "reason_code": "login_timeout", "is_demo": None,
                       "_operator": {"initialize_started": True, "initialize_result": False,
                                     "last_error_code": -10005, "last_error_text": "IPC timeout",
                                     "account_info_present": False}}
            win = _FakeWin(procs=[{"pid": 10804, "session": 0, "path": "x"}], remaining=[])
            cfg, rid, status = self._run(tmp, outcome, win=win,
                                         milestones=["TERMINAL_STARTED", "BROKER_TCP_ESTABLISHED"])
            self.assertEqual(status, "ok")
            res = _read_result(cfg["validation_handoff_dir"], rid)
            self.assertEqual(res["reason_code"], "login_timeout")
            self.assertNotIn("_operator", res)                       # customer body has no operator internals
            self.assertIn("operator", res)                           # but the allow-listed summary is present
            ev = _read_evidence(cfg["validation_diagnostics_dir"], "corr-1")
            self.assertEqual(ev["terminal_pid"], 10804)
            self.assertEqual(ev["last_error_code"], -10005)
            self.assertEqual(ev["first_failing_stage"], "BROKER_AUTHORISED")
            self.assertTrue(ev["broker_tcp_observed"])
            self.assertEqual(ev["stray_termination_result"]["killed"], [10804])
            self.assertTrue(ev["terminal_exited_after_shutdown"])
            self.assertEqual(ev["baseline_restore_result"], "restored")
            self.assertTrue(win.terminate_calls)                     # termination was attempted

    def test_lingering_terminal_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            win = _FakeWin(procs=[{"pid": 5, "session": 0, "path": "x"}], remaining=[5])   # survives
            cfg, rid, _ = self._run(tmp, {"ok": False, "reason_code": "login_timeout", "is_demo": None,
                                          "_operator": {"initialize_started": True}}, win=win)
            ev = _read_evidence(cfg["validation_diagnostics_dir"], "corr-1")
            self.assertFalse(ev["terminal_exited_after_shutdown"])   # lingering terminal is recorded, not hidden

    def test_success_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = {"ok": True, "reason_code": "demo_ok", "is_demo": True,
                       "_operator": {"initialize_started": True, "initialize_result": True,
                                     "account_info_present": True, "trade_mode": 0}}
            cfg, rid, status = self._run(tmp, outcome, milestones=["TERMINAL_STARTED", "BROKER_AUTHORISED"])
            res = _read_result(cfg["validation_handoff_dir"], rid)
            self.assertTrue(res["ok"])
            self.assertEqual(res["is_demo"], True)
            ev = _read_evidence(cfg["validation_diagnostics_dir"], "corr-1")
            self.assertEqual(ev["stage_reached"], "CLASSIFIED")

    def test_scrub_always_runs_and_removes_accounts_dat(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            adat = os.path.join(cfg["validation_terminal_dir"], "config", "accounts.dat")
            open(adat, "w").write("secret-credential-blob")
            logs = os.path.join(cfg["validation_terminal_dir"], "logs")
            os.makedirs(logs, exist_ok=True)
            open(os.path.join(logs, "x.log"), "w").write("journal")
            rid = _stage_request(cfg["validation_handoff_dir"])
            runner.run_once(cfg, build_handler=lambda _c: _FakeHandler(
                {"ok": False, "reason_code": "login_timeout", "is_demo": None, "_operator": {}}),
                win=_FakeWin(), clock=lambda: 1.0, read_journal=lambda d: [],
                mirror_baseline=lambda s, d: "restored")
            self.assertFalse(os.path.exists(adat))                   # credential artefact removed
            self.assertFalse(os.path.isdir(logs))                    # logs removed

    def test_diagnostic_capture_failure_still_scrubs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            adat = os.path.join(cfg["validation_terminal_dir"], "config", "accounts.dat")
            open(adat, "w").write("cred")
            rid = _stage_request(cfg["validation_handoff_dir"])

            def _boom(*_a, **_k):
                raise RuntimeError("evidence write exploded (e.g. disk full)")

            orig = diag.write_evidence
            diag.write_evidence = _boom
            try:
                status = runner.run_once(cfg, build_handler=lambda _c: _FakeHandler(
                    {"ok": False, "reason_code": "login_timeout", "is_demo": None, "_operator": {}}),
                    win=_FakeWin(), clock=lambda: 1.0, read_journal=lambda d: [],
                    mirror_baseline=lambda s, d: "restored")
            finally:
                diag.write_evidence = orig
            self.assertEqual(status, "diagnostic_capture_failed")
            res = _read_result(cfg["validation_handoff_dir"], rid)
            self.assertEqual(res["reason_code"], "diagnostic_capture_failed")   # never a silent login_timeout
            self.assertFalse(res["ok"])
            self.assertFalse(os.path.exists(adat))                              # emergency scrub still ran

    def test_termination_scoped_to_terminal_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            win = _FakeWin(procs=[{"pid": 1, "session": 0, "path": "x"}])
            cfg, _rid, _s = self._run(tmp, {"ok": False, "reason_code": "login_timeout", "is_demo": None,
                                            "_operator": {}}, win=win)
            # the runner asked the adapter to terminate ONLY within the configured validation terminal dir
            vdir, forbidden = win.terminate_calls[0]
            self.assertEqual(vdir, cfg["validation_terminal_dir"])
            self.assertIn(r"C:\GuvFX\beta\slots", forbidden)         # forbidden roots include the slot tree

    def test_no_single_pending_when_two_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            _stage_request(cfg["validation_handoff_dir"], correlation_id="a")
            _stage_request(cfg["validation_handoff_dir"], correlation_id="b")
            status = runner.run_once(cfg, build_handler=lambda _c: _FakeHandler({}), win=_FakeWin(),
                                     clock=lambda: 1.0, read_journal=lambda d: [],
                                     mirror_baseline=lambda s, d: "restored")
            self.assertTrue(status.startswith("no_single_pending:2"))


# ── secret safety at the runner boundary ────────────────────────────────────────────────────────────────
class RunnerSecretSafetyTests(unittest.TestCase):
    def test_operator_summary_only_allowlisted_keys_in_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            rid = _stage_request(cfg["validation_handoff_dir"])
            runner.run_once(cfg, build_handler=lambda _c: _FakeHandler(
                {"ok": False, "reason_code": "login_timeout", "is_demo": None,
                 "_operator": {"last_error_text": "Authorization failed"}}),
                win=_FakeWin(procs=[{"pid": 9, "session": 0, "path": "x"}]),
                clock=lambda: 1.0, read_journal=lambda d: [], mirror_baseline=lambda s, d: "restored")
            res = _read_result(cfg["validation_handoff_dir"], rid)
            self.assertTrue(set(res["operator"].keys()).issubset(set(handoff._OPERATOR_KEYS)))

    def test_probe_operator_leaks_no_password(self):
        # A handler whose _operator accidentally contains a password → build_evidence must scrub it.
        ev = diag.build_evidence({"last_error_reason": "password=hunter2", "reason_code": "login_timeout"})
        self.assertNotIn("hunter2", json.dumps(ev))


if __name__ == "__main__":
    unittest.main()
