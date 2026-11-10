# Resumable PostgreSQL -> Azure embeddings, Windows / PowerShell.
# Default immbot profile defines RPM=30, TPM=750000, batch=128, tokens=24000.
# RPM is a cautious pilot cap, NOT a verified gateway RPM entitlement.
param(
    [string]$Profile = ".\profiles\immbot_approved_1536.json",
    [ValidateSet("pilot", "full", "dry-run")][string]$Mode = "pilot",
    [int]$Workers = 4,
    [int]$MaxFiles = 1000,
    [int]$Rpm = 0,
    [int]$Tpm = 0,
    [int]$BatchSize = 0,
    [int]$BatchTokens = 0,
    [int]$MaxAttempts = 0,
    [int]$TimeoutSeconds = 0,
    [int]$LeaseSeconds = 0,
    [switch]$ConfirmFull,
    [switch]$RetryFailed,
    [switch]$Rescan,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if ($Workers -lt 1 -or $Workers -gt 128) { throw "Workers must be 1..128" }
if (-not (Test-Path $Profile)) { throw "Profile not found: $Profile" }

if (-not $Python) {
    $primary = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
    $alternate = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (Test-Path $primary) { $Python = $primary }
    elseif (Test-Path $alternate) { $Python = $alternate }
    else { $Python = "python" }
}

$arguments = @("-u", "main.py", "--embed-existing", "--profile", $Profile,
               "--doc-workers", [string]$Workers)

foreach ($item in @(
    @("embedding.rpm", $Rpm),
    @("embedding.tpm", $Tpm),
    @("embedding.batch_size", $BatchSize),
    @("embedding.batch_tokens", $BatchTokens),
    @("embedding.max_attempts", $MaxAttempts),
    @("embedding.timeout_seconds", $TimeoutSeconds),
    @("embedding.lease_seconds", $LeaseSeconds)
)) {
    if ($item[1] -lt 0) { throw "$($item[0]) must not be negative" }
    if ($item[1] -gt 0) { $arguments += @("--set", "$($item[0])=$($item[1])") }
}

switch ($Mode) {
    "pilot" {
        if ($MaxFiles -lt 1) { throw "Pilot MaxFiles must be positive" }
        $arguments += @("--max-files", [string]$MaxFiles)
    }
    "full" {
        if (-not $ConfirmFull) { throw "Full backfill requires -Mode full -ConfirmFull" }
    }
    "dry-run" { $arguments += "--dry-run" }
}

if ($RetryFailed) { $arguments += "--retry-embed-failures" }
if ($Rescan) { $arguments += "--rescan-embeddings" }

Write-Host "Embedding mode=$Mode profile=$Profile workers=$Workers"
Write-Host "RPM/TPM and batch defaults come from profile unless overridden."
& $Python @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
