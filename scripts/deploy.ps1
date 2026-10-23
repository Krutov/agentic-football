<#
.SYNOPSIS
    Test and deploy the football agent to Amazon Bedrock AgentCore Runtime.

.DESCRIPTION
    1. Checks the venv, agentcore CLI and AWS credentials.
    2. Runs the offline test suite (python -m tests.run) unless -SkipTests.
    3. Configures (first run only) and deploys one agent, or five identical
       agents (-Players 5) if the Player Portal refuses the same ARN 5 times.
    4. Prints the runtime ARNs and saves them to arns.txt.

.EXAMPLE
    .\scripts\deploy.ps1
.EXAMPLE
    .\scripts\deploy.ps1 -AgentEnv "LOG_RAW_PAYLOAD=true"
.EXAMPLE
    .\scripts\deploy.ps1 -Players 5 -AgentEnv "USE_LLM_COACH=true","RESPONSE_WRAPPER=commands"
#>
[CmdletBinding()]
param(
    [ValidateSet(1, 5)]
    [int]$Players = 1,

    [ValidatePattern('^[a-zA-Z][a-zA-Z0-9_]{0,40}$')]
    [string]$Name = "football",

    [string]$Region = "us-east-1",

    [ValidateSet("container", "direct_code_deploy")]
    [string]$DeploymentType = "container",

    # Runtime env vars for the agent, KEY=VALUE each
    [string[]]$AgentEnv = @(),

    [switch]$SkipTests,

    # Re-run 'agentcore configure' even if the agent is already configured
    [switch]$Reconfigure
)

# Native tools write progress to stderr; failures are detected via $LASTEXITCODE instead
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Fail([string]$Text) { Write-Host "ERROR: $Text" -ForegroundColor Red; exit 1 }

$env:AGENTCORE_SUPPRESS_RECOMMENDATION = "1"
$env:PYTHONUTF8 = "1"
if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = $Region }

# 1. Environment checks
Write-Step "Checking environment"
if (-not $env:VIRTUAL_ENV) {
    $activate = Join-Path $repoRoot ".venv\Scripts\Activate.ps1"
    if (Test-Path $activate) {
        . $activate
        Write-Host "Activated .venv"
    } else {
        Fail "No virtualenv. Run: python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt bedrock-agentcore-starter-toolkit"
    }
}
if (-not (Get-Command agentcore -ErrorAction SilentlyContinue)) {
    Fail "agentcore CLI not found. Run: pip install bedrock-agentcore-starter-toolkit"
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Fail "AWS CLI not found. Run: winget install -e --id Amazon.AWSCLI"
}

$identityJson = aws sts get-caller-identity --output json 2>&1
if ($LASTEXITCODE -ne 0) {
    Fail "AWS credentials are not valid: $identityJson`nSet AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN from the workshop account."
}
$identity = $identityJson | ConvertFrom-Json
Write-Host "AWS account: $($identity.Account)  identity: $($identity.Arn)  region: $Region"

# 2. Offline tests
if (-not $SkipTests) {
    Write-Step "Running offline tests"
    python -m tests.run
    if ($LASTEXITCODE -ne 0) { Fail "Tests failed, deploy aborted (use -SkipTests to override)" }
}

# 3. Configure + deploy
$names = if ($Players -eq 1) { @($Name) } else { 0..4 | ForEach-Object { "${Name}_p$_" } }
$configured = python scripts\agent_arns.py | ConvertFrom-Json

foreach ($agentName in $names) {
    $isConfigured = $configured.PSObject.Properties.Name -contains $agentName

    if ($Reconfigure -or -not $isConfigured) {
        Write-Step "Configuring $agentName ($DeploymentType)"
        $cfgArgs = @("configure", "-e", "agent.py", "-n", $agentName, "-r", $Region,
                     "-rf", "requirements.txt", "-dt", $DeploymentType, "-ni")
        if ($DeploymentType -eq "direct_code_deploy") { $cfgArgs += @("-rt", "PYTHON_3_12") }
        & agentcore @cfgArgs
        if ($LASTEXITCODE -ne 0) { Fail "agentcore configure failed for $agentName" }
    }

    Write-Step "Deploying $agentName"
    $deployArgs = @("deploy", "-a", $agentName, "--auto-update-on-conflict")
    foreach ($kv in $AgentEnv) {
        if ($kv -notmatch '^[A-Za-z_][A-Za-z0-9_]*=') { Fail "Bad -AgentEnv value '$kv', expected KEY=VALUE" }
        $deployArgs += @("-env", $kv)
    }
    & agentcore @deployArgs
    if ($LASTEXITCODE -ne 0) { Fail "agentcore deploy failed for $agentName" }
}

# 4. ARNs
Write-Step "Runtime ARNs"
$arns = python scripts\agent_arns.py | ConvertFrom-Json
$lines = foreach ($agentName in $names) {
    $arn = $arns.$agentName
    if (-not $arn) { Write-Host "WARNING: no ARN recorded for $agentName, check 'agentcore status -a $agentName'" -ForegroundColor Yellow }
    "{0}`t{1}" -f $agentName, $arn
}
$lines | Set-Content -Encoding utf8 arns.txt
$lines | ForEach-Object { Write-Host $_ -ForegroundColor Green }

if ($Players -eq 1) {
    Write-Host "`nRegister this ARN for all 5 players in Player Portal -> My Team."
    Write-Host "If the portal rejects duplicates, run: .\scripts\deploy.ps1 -Players 5"
} else {
    Write-Host "`nRegister ${Name}_p0 as the goalkeeper (player 0), then p1..p4 in order."
}
Write-Host "Saved to arns.txt"
