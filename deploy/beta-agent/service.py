"""CVM-Inc-3 B3P-1 — Windows SCM wrapper (pywin32) for the beta provisioning agent (verification B-5).

``agent.py`` is a bare HTTP server with no Service Control Manager harness, so a raw ``sc create`` pointing at
it fails SCM start (error 1053 — "did not respond to start in a timely fashion"). This module wraps the
testable ``AgentServer`` in a ``win32serviceutil.ServiceFramework`` so the SCM can start/stop/report it as a
real service, and so stop performs the DRAIN (verification B-6).

Windows-only: the pywin32 import is guarded so this module can be imported off-box for inspection, but the
service class + install only work on the host. The test suite never imports this module — all real logic is in
``AgentServer`` (``agent.py``), which is fully unit-tested off-box.

Install (B3, after merge + approval):  python service.py install   /   python service.py start
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

try:  # pragma: no cover — Windows-only dependency
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
    _PYWIN32 = True
except ImportError:
    _PYWIN32 = False

import config as agent_config          # noqa: E402  stdlib-only, safe off-box
from agent import AgentServer          # noqa: E402  stdlib-only, safe off-box


def pywin32_available() -> bool:
    """True only on the host where the SCM wrapper can actually run (used by validate/tests to skip)."""
    return _PYWIN32


if _PYWIN32:  # pragma: no cover — exercised only on the Windows host during B3

    class GuvFXBetaAgentService(win32serviceutil.ServiceFramework):
        _svc_name_ = "GuvFXBetaAgent"
        _svc_display_name_ = "GuvFX Beta Provisioning Agent"
        _svc_description_ = ("Private-network provisioning agent for beta MT5 runtimes. Binds only to the "
                             "Tailscale management address; does not interact with the trade bridge (:8788).")

        def __init__(self, args):
            super().__init__(args)
            self._stop_evt = win32event.CreateEvent(None, 0, 0, None)
            self._server = None
            self._drain_timeout_s = 20.0

        def SvcStop(self):
            # Report STOP_PENDING with a wait hint that covers the drain so the SCM does not kill us mid-drain.
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING,
                                     waitHint=int((self._drain_timeout_s + 10) * 1000))
            if self._server is not None:
                drained = self._server.stop()
                if not drained:
                    servicemanager.LogWarningMsg("GuvFXBetaAgent: mutation drain timed out; forced stop")
            win32event.SetEvent(self._stop_evt)

        def SvcDoRun(self):
            cfg = agent_config.load_config()             # pins the exact private bind + refuses reserved ports
            self._drain_timeout_s = float(cfg.get("drain_timeout_s", 20.0))
            self._server = AgentServer(cfg, enforce_integrity=True)
            self._server.start()
            servicemanager.LogInfoMsg("GuvFXBetaAgent: started (bind %s:%s)"
                                      % (cfg["bind_host"], cfg["bind_port"]))
            win32event.WaitForSingleObject(self._stop_evt, win32event.INFINITE)
            servicemanager.LogInfoMsg("GuvFXBetaAgent: stopped")


def main(argv=None):
    if not _PYWIN32:
        sys.stderr.write("pywin32 not available — the SCM wrapper runs only on the Windows host\n")
        return 2
    win32serviceutil.HandleCommandLine(GuvFXBetaAgentService, argv=argv)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
