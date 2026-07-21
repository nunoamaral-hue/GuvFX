# CVM-Inc-3 B2 — host firewall rule: allow the agent port ONLY from the GuvFX control-plane source, on the
# private/Tailscale interface. DARK ARTEFACT: RUN ONLY in B3. No public exposure, no reverse proxy.
param(
  [string]$RuleName   = "GuvFX-Beta-Agent-In",
  [int]$Port          = 8791,
  [string]$AllowFrom  = "100.119.23.29",     # GuvFX backend / control plane (Tailscale)
  [string]$Interface  = "100.79.101.19"      # private/Tailscale bind address
)
Write-Host "Would add inbound allow: TCP $Port on $Interface from $AllowFrom only; default-deny otherwise."
Write-Host "DO NOT run in B2."
