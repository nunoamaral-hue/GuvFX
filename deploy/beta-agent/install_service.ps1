# CVM-Inc-3 B2 — install the beta provisioning agent as a Windows service (min-privilege identity).
# DARK ARTEFACT: reviewed + shipped in B2; RUN ONLY in B3 after merge. Does NOT touch Session 3, the
# prod terminal, the bridge, port 8788, autologon or startup tasks. Requires an already-provisioned,
# NON-admin service account and the secrets set as machine env vars via the Windows secret store.
param(
  [string]$ServiceName = "GuvFXBetaAgent",
  [string]$AgentDir    = "C:\GuvFX\beta\agent",
  [string]$Python      = "C:\Python311\python.exe",
  [string]$RunAsUser   = "GUVFX\svc_beta_agent"   # NON-admin, least-privilege
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path (Join-Path $AgentDir "agent.py"))) { throw "agent.py not found under $AgentDir" }
# nssm-style service creation is environment-specific; this is the documented install shape, not executed here.
Write-Host "Would create service '$ServiceName' running '$Python $AgentDir\agent.py' as $RunAsUser."
Write-Host "Bind host/port + keyring come from machine env (BETA_AGENT_*). Verify assert_private_bind passes."
Write-Host "DO NOT run in B2. B3 performs the controlled install."
