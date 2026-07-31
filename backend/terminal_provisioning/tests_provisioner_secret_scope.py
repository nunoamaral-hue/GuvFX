"""Phase B - provisioner-only secret scope conformance.

The beta HMAC signing keyring must reach ONLY guvfx-beta-provisioner, never the public api.guvfx.com
backend (RULE 3, least privilege). These off-VPS checks pin that the compose overlay scopes a
provisioner-only env_file, that no real secret is committed anywhere, that the keyring loads from the
process env (not hardcoded), and that the deploy runbook carries the no-reveal verification and the
cleartext-leak warning. The deploy-time proof (backend env clean, provisioner env scoped) lives in
deploy/beta-provisioner/DEPLOY_SECRET_SCOPE.md and is run on the VPS without revealing the value.
"""
import os
import re
import subprocess

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PROV = os.path.join(_REPO, "deploy", "beta-provisioner")
OVERLAY = os.path.join(_PROV, "docker-compose.beta-provisioner.yml")
TEMPLATE = os.path.join(_PROV, "beta-provisioner.secret.env.example")
RUNBOOK = os.path.join(_PROV, "DEPLOY_SECRET_SCOPE.md")
SECRET_KEYS = ("BETA_AGENT_KEYRING", "BETA_AGENT_KEY_ID")
_SKIP_DIRS = {".git", "node_modules", ".venv", ".next", "__pycache__", "dist", "build", ".mypy_cache"}


def _read(p):
    return open(p, encoding="utf-8").read()


def _yaml_code_lines(text):
    """Comment-stripped executable lines of a YAML file (drop full-line and inline `#`)."""
    out = []
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        out.append(raw.split(" #", 1)[0].rstrip())
    return out


class ProvisionerSecretScopeTests(SimpleTestCase):
    def test_overlay_scopes_the_env_file_to_the_provisioner_service_only(self):
        # Structural (no pyyaml): the env_file key must live inside the guvfx-beta-provisioner service block,
        # and its value must reference the provisioner-only secret file. There is exactly one service here.
        lines = _yaml_code_lines(_read(OVERLAY))
        self.assertIn("services:", lines)
        self.assertTrue(any(l.strip() == "guvfx-beta-provisioner:" for l in lines), "service missing")
        # collect the service block (everything indented under the single service)
        idx = next(i for i, l in enumerate(lines) if l.strip() == "guvfx-beta-provisioner:")
        block = "\n".join(lines[idx + 1:])
        self.assertIn("env_file:", block, "env_file: must be a real key on the provisioner service")
        self.assertIn("beta-provisioner.secret.env", block, "env_file must reference the secret file")
        # and the value uses the project-dir-relative default (the reviewed, resolvable path)
        self.assertRegex(block, r"BETA_PROVISIONER_SECRET_ENV:-deploy/beta-provisioner/beta-provisioner\.secret\.env")

    def test_overlay_has_no_inline_secret_and_no_environment_block(self):
        text = _read(OVERLAY)
        for key in SECRET_KEYS:
            self.assertNotRegex(text, rf"{key}\s*[:=]\s*\S", f"{key} has an inline value in the overlay")
        self.assertNotIn("environment:", "\n".join(_yaml_code_lines(text)),
                         "overlay must stay env_file-only (an environment: block would shadow the env_file)")

    def test_template_has_the_required_keys_with_empty_secrets(self):
        text = _read(TEMPLATE)
        self.assertRegex(text, r"(?m)^BETA_AGENT_BASE_URL=\S", "non-secret base_url should be pre-filled")
        for key in SECRET_KEYS:
            self.assertRegex(text, rf"(?m)^{key}=\s*$", f"{key} must be present but EMPTY in the template")

    def test_no_real_secret_value_is_committed_anywhere_in_the_repo(self):
        # Whole-repo scan (not just deploy/): flag a NON-EMPTY BETA_AGENT_KEYRING/KEY_ID assignment at line
        # start in any committed text file. The .example is scanned too (its values are empty, so it passes);
        # comment lines and inline `# ...` are stripped so only a genuinely-assigned value trips this.
        offenders = []
        for root, dirs, files in os.walk(_REPO):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    body = open(p, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                for raw in body.splitlines():
                    line = raw.strip()
                    if line.startswith("#"):
                        continue
                    for key in SECRET_KEYS:
                        m = re.match(rf"{key}\s*[:=]\s*(.*)$", line)
                        if not m:
                            continue
                        val = m.group(1).split("#", 1)[0].strip().strip("\"'")
                        # ignore the compose interpolation default reference (not a value)
                        if val and not val.startswith("${"):
                            offenders.append(f"{os.path.relpath(p, _REPO)}: {key}={val[:6]}...")
        self.assertEqual(offenders, [], f"committed non-empty secret: {offenders}")

    def test_keyring_loads_from_the_process_env_and_is_not_hardcoded(self):
        # Behavioral: _load_keyring returns the env value when set, and EMPTY when unset (no hardcoded secret).
        from terminal_provisioning import mgmt_client
        saved = {k: os.environ.get(k) for k in SECRET_KEYS}
        try:
            os.environ["BETA_AGENT_KEYRING"] = '{"kid-unit-test": "s"}'
            os.environ["BETA_AGENT_KEY_ID"] = "kid-unit-test"
            kr, kid = mgmt_client._load_keyring()
            self.assertEqual(kid, "kid-unit-test")
            self.assertIn("kid-unit-test", kr)
            os.environ.pop("BETA_AGENT_KEYRING")
            os.environ.pop("BETA_AGENT_KEY_ID")
            kr2, kid2 = mgmt_client._load_keyring()
            self.assertEqual(kr2, {}, "unset keyring must yield {} (no hardcoded default)")
            self.assertEqual(kid2, "")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_gitignore_ignores_the_real_secret_but_tracks_the_example(self):
        try:
            def ignored(rel):
                r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=_REPO,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return r.returncode == 0
            self.assertTrue(ignored("deploy/beta-provisioner/beta-provisioner.secret.env"),
                            "the real secret file must be git-ignored")
            self.assertFalse(ignored("deploy/beta-provisioner/beta-provisioner.secret.env.example"),
                             "the .example template must be tracked")
        except (OSError, FileNotFoundError):
            self.skipTest("git not available")

    def test_the_real_secret_file_is_not_present_in_the_repo(self):
        self.assertFalse(os.path.exists(os.path.join(_PROV, "beta-provisioner.secret.env")),
                         "the real secret file must only ever exist on the VPS, never in the repo")

    def test_deploy_runbook_has_the_sponsor_step_no_reveal_checks_and_leak_warning(self):
        rb = _read(RUNBOOK)
        self.assertIn("SPONSOR-ONLY STEP", rb)
        self.assertIn("BACKEND_CLEAN", rb)            # least-privilege verification (backend has no key)
        self.assertIn("PROVISIONER_SCOPED", rb)       # provisioner-has-it verification (presence only)
        self.assertIn("CLEARTEXT", rb)                # the docker-compose-config leak warning
        self.assertIn("compose project directory", rb)   # the corrected env_file path rule
        self.assertNotIn("grep -A2 -i env_file", rb)  # the broken/misleading discovery command was removed
