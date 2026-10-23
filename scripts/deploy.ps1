<#
.SYNOPSIS
    Deploy the football agent as 5 AgentCore runtimes (football_p0..p4).

.DESCRIPTION
    Thin wrapper around team\deploy.py, which uses the same pipeline as the
    official workshop sample teams (AgentCore CLI + CDK).
    Activates .venv if needed, then runs tests and deploys.

.EXAMPLE
    .\scripts\deploy.ps1
.EXAMPLE
    .\scripts\deploy.ps1 -SkipTests
#>
[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$env:PYTHONUTF8 = "1"
if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = "us-east-1" }

if (-not $env:VIRTUAL_ENV) {
    $activate = Join-Path $repoRoot ".venv\Scripts\Activate.ps1"
    if (Test-Path $activate) { . $activate; Write-Host "Activated .venv" }
}

$deployArgs = @("team\deploy.py")
if ($SkipTests) { $deployArgs += "--skip-tests" }
python @deployArgs
exit $LASTEXITCODE
