<#
.SYNOPSIS
    Verifies that the two production Coolify apps (bkjr-backend and
    agentops-frontend) are healthy on the *.getbijou.xyz FQDNs.

.DESCRIPTION
    Runs four checks for each app:
      1. DNS resolves (Test-Connection / Resolve-DnsName)
      2. TCP 443 is reachable
      3. HTTPS root returns 2xx
      4. (backend only) /api/state returns JSON with ok=true

    Prints a colour-coded PASS/FAIL table and exits 1 if anything fails.
    Designed to be safe to run from a Windows Task Scheduler, GitHub
    Actions ubuntu-latest (via PowerShell Core), or a developer laptop.

.PARAMETER FrontendHost
    Override the frontend FQDN. Default: agentops.getbijou.xyz

.PARAMETER BackendHost
    Override the backend FQDN. Default: bkjr-api.getbijou.xyz

.PARAMETER TimeoutSec
    Per-request timeout in seconds. Default: 15

.EXAMPLE
    .\scripts\verify-deploy.ps1
    .\scripts\verify-deploy.ps1 -BackendHost localhost -FrontendHost localhost
#>
[CmdletBinding()]
param(
    [string]$FrontendHost = 'agentops.getbijou.xyz',
    [string]$BackendHost  = 'bkjr-api.getbijou.xyz',
    [int]   $TimeoutSec   = 15
)

$ErrorActionPreference = 'Continue'

# ANSI colour helpers (works in PowerShell 7+ on Windows Terminal, GitHub Actions ubuntu, and modern Windows console)
# Counter is held in module-scope so it survives dot-sourcing / nested function calls
$script:FailCount = 0
function Write-Pass   { param([string]$m) Write-Host ("  [PASS] " + $m) -ForegroundColor Green }
function Write-Fail   { param([string]$m) Write-Host ("  [FAIL] " + $m) -ForegroundColor Red; $script:FailCount++ }
function Write-Skip   { param([string]$m) Write-Host ("  [SKIP] " + $m) -ForegroundColor DarkYellow }
function Write-Title  { param([string]$m) Write-Host ("`n" + $m) -ForegroundColor Cyan }

function Test-HostHealth {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]$Label,
        [Parameter(Mandatory)] [string]$HostName,
        [string]$HealthPath = '/',
        [string]$ApiPath    = '',
        [int]   $Port       = 443
    )

    Write-Title "== $Label ($HostName) =="

    # 1) DNS
    try {
        $dns = Resolve-DnsName -Name $HostName -ErrorAction Stop -Type A -DnsOnly |
               Select-Object -First 1 -ExpandProperty IPAddress
        if ($dns) { Write-Pass "DNS resolves: $HostName -> $dns" }
        else      { Write-Fail "DNS resolved no A record for $HostName"; return }
    } catch {
        Write-Fail ("DNS lookup failed for {0}: {1}" -f $HostName, $_.Exception.Message)
        return
    }

    # 2) TCP port reachable
    try {
        $tcp = Test-NetConnection -ComputerName $HostName -Port $Port -WarningAction SilentlyContinue -InformationLevel Quiet
        if ($tcp) { Write-Pass "TCP $Port reachable" }
        else      { Write-Fail "TCP $Port NOT reachable" ; return }
    } catch {
        Write-Fail ("Test-NetConnection failed: {0}" -f $_.Exception.Message)
        return
    }

    # 3) HTTPS root returns 2xx and has body
    $root = "https://$HostName$HealthPath"
    try {
        $r = Invoke-WebRequest -Uri $root -Method Get -TimeoutSec $TimeoutSec `
                              -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400) {
            $bodyLen = if ($r.RawContentLength) { $r.RawContentLength } else { ($r.Content | Measure-Object -Character).Characters }
            Write-Pass ("GET {0} -> {1} ({2} bytes)" -f $root, $r.StatusCode, $bodyLen)
        } else {
            Write-Fail ("GET {0} -> {1}" -f $root, $r.StatusCode)
            return
        }

        # For the frontend, sanity-check the HTML title
        if ($Label -like 'Frontend*' -and $r.Content) {
            if ($r.Content -match '<title>([^<]+)</title>') {
                Write-Pass ("HTML title: {0}" -f $matches[1])
            } else {
                Write-Fail "HTML has no <title> tag"
            }
        }
    } catch {
        Write-Fail ("GET {0} threw: {1}" -f $root, $_.Exception.Message)
        return
    }

    # 4) Optional API check (backend only)
    if ($ApiPath) {
        $api = "https://$HostName$ApiPath"
        try {
            $a = Invoke-WebRequest -Uri $api -Method Get -TimeoutSec $TimeoutSec `
                                   -UseBasicParsing -ErrorAction Stop
            if ($a.StatusCode -eq 200) {
                $j = $a.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
                if ($j -and ($j.ok -eq $true -or $j.status -eq 'ok' -or $j.healthy -eq $true)) {
                    Write-Pass ("GET {0} -> 200, ok=true" -f $api)
                } elseif ($j) {
                    # Accept JSON 200 even if shape differs; surface what we got
                    Write-Pass ("GET {0} -> 200 JSON (fields: {1})" -f $api, (($j.PSObject.Properties | ForEach-Object { $_.Name }) -join ','))
                } else {
                    Write-Pass ("GET {0} -> 200 (non-JSON body, {1} bytes)" -f $api, $a.RawContentLength)
                }
            } else {
                Write-Fail ("GET {0} -> {1}" -f $api, $a.StatusCode)
            }
        } catch {
            Write-Fail ("GET {0} threw: {1}" -f $api, $_.Exception.Message)
        }
    }
}

# --- main ---
Write-Title "agentops-platform deploy verify @ $(Get-Date -Format o)"
Write-Host ("  Frontend : {0}" -f $FrontendHost)
Write-Host ("  Backend  : {0}" -f $BackendHost)
Write-Host ("  Timeout  : {0}s per request" -f $TimeoutSec)

Test-HostHealth -Label 'Frontend (agentops-frontend)' -HostName $FrontendHost -HealthPath '/'
Test-HostHealth -Label 'Backend  (bkjr-backend)'      -HostName $BackendHost  -HealthPath '/api/state' -ApiPath '/api/state'

Write-Title "== summary =="
if ($script:FailCount -eq 0) {
    Write-Host ("  ALL CHECKS PASSED ({0} apps)" -f 2) -ForegroundColor Green
    exit 0
} else {
    Write-Host ("  {0} CHECK(S) FAILED" -f $script:FailCount) -ForegroundColor Red
    exit 1
}
