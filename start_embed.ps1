# PowerShell: distributed embedding worker (run on any Windows host).
param(
 [string]$Profile = ".\profiles\clinical_1536.json",
 [int]$Workers = 16,
 [int]$BatchSize = 128,
 [int]$Rpm = 0,
 [int]$Tpm = 0,
 [switch]$RetryFailed,
 [switch]$Rescan
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$arguments = @("-u","main.py","--embed-existing","--profile",$Profile,
 "--doc-workers",[string]$Workers,"--set","embedding.batch_size=$BatchSize")
if ($Rpm -gt 0) { $arguments += @("--set","embedding.rpm=$Rpm") }
if ($Tpm -gt 0) { $arguments += @("--set","embedding.tpm=$Tpm") }
if ($RetryFailed) { $arguments += "--retry-embed-failures" }
if ($Rescan) { $arguments += "--rescan-embeddings" }
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
& $python @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
