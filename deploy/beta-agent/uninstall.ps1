# CVM-Inc-3 B2 — rollback: stop + remove the beta agent service and its firewall rule. Leaves runtimes,
# tombstones and Nuno's estate untouched. Never deletes beta runtime data (tombstones are retained).
param([string]$ServiceName = "GuvFXBetaAgent", [string]$RuleName = "GuvFX-Beta-Agent-In")
Write-Host "Would stop + delete service '$ServiceName' and remove firewall rule '$RuleName'."
Write-Host "Runtime + tombstone directories are RETAINED (no deletion)."
