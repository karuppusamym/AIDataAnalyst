<#
.SYNOPSIS
  Run one Atlas UI per demo user, each with that user's own development identity.

.DESCRIPTION
  The shipped UI bakes ONE development identity into its bundle (`local-ui-admin` holding every
  role), so the container on :3001 cannot show two different users. This starts a Vite dev server
  per demo user instead. Each server proxies /v1 to the running API and sends that user's
  `X-Principal-Id` / `X-Roles`, so the BACKEND decides what the user may do -- nothing is faked
  in the browser. Each UI also starts as that user's persona in the Northwind organization
  (`VITE_DEV_PERSONA`, `VITE_DEV_ORG_ID`), so nothing has to be chosen or pasted first: a
  user who cannot list organizations has no picker to choose one with. Separate ports are
  separate browser origins, so each tab keeps its own selections afterwards.

  Development identity only: it needs the API to run `identity_provider=development` (the local
  stack). For the OIDC-shaped path (a real sign-in with claims) see compose.oidc.yaml and
  Docs/walkthrough/roles-and-users.html.

  Nothing here writes to the stack or installs anything; it needs ui-next/node_modules to exist.

.EXAMPLE
  .\scripts\demo-users.ps1                        # list the roster
  .\scripts\demo-users.ps1 -Action Start          # start all eight
  .\scripts\demo-users.ps1 -Action Start -Users dana.steward,riya.reviewer
  .\scripts\demo-users.ps1 -Action Check          # confirm each server serves its own identity
  .\scripts\demo-users.ps1 -Action Stop
#>
param(
    [ValidateSet("List", "Start", "Check", "Stop")][string]$Action = "List",
    [string[]]$Users = @(),
    [string]$ApiUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repo = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $env:TEMP "atlas-demo-users.json"

# name, port, persona (the shell's navigation mode), roles. Role bundles follow the
# `atlas-steward` bundle in compose.oidc.yaml: a working user holds Analyst and Viewer as well.
$roster = @(
    @{ Name = "alex.operator";  Port = 5181; Persona = "Operator";
       Roles = "PlatformAdmin,OrganizationAdmin,MetadataAdmin,DataAdmin,SemanticAdmin,DataSteward,Reviewer,MetadataReviewer,Auditor,Operations,Analyst,Viewer,ToolDeveloper,ToolConsumer,AgentDeveloper,MetadataIngestor" },
    @{ Name = "dana.steward";   Port = 5182; Persona = "Steward";
       Roles = "DataSteward,MetadataReviewer,Analyst,Viewer" },
    @{ Name = "riya.reviewer";  Port = 5183; Persona = "Reviewer";
       Roles = "Reviewer,Viewer" },
    @{ Name = "omar.auditor";   Port = 5184; Persona = "Auditor";
       Roles = "Auditor,Viewer" },
    @{ Name = "ana.analyst";    Port = 5185; Persona = "Analyst";
       Roles = "Analyst,Viewer" },
    @{ Name = "vic.viewer";     Port = 5186; Persona = "Analyst";
       Roles = "Viewer" },
    @{ Name = "ravi.dataadmin"; Port = 5187; Persona = "Operator";
       Roles = "DataAdmin,Viewer" },
    @{ Name = "sam.agentdev";   Port = 5188; Persona = "Analyst";
       Roles = "AgentDeveloper,ToolDeveloper,Analyst,Viewer" }
)

function Select-Roster {
    if ($Users.Count -eq 0) { return $roster }
    $unknown = $Users | Where-Object { $_ -notin ($roster | ForEach-Object { $_.Name }) }
    if ($unknown) { throw "Unknown user(s): $($unknown -join ', '). Run with no -Action to list them." }
    return $roster | Where-Object { $_.Name -in $Users }
}

function Get-OrgId {
    # The sample organization's id, from the API. Only PlatformAdmin may list every organization.
    $headers = @{ "X-Principal-Id" = "demo-launcher"; "X-Roles" = "PlatformAdmin" }
    $orgs = (Invoke-RestMethod -Uri "$ApiUrl/v1/organizations" -Headers $headers).items
    $sample = $orgs | Where-Object { $_.slug -eq "sample-bank" } | Select-Object -First 1
    if ($null -eq $sample) { throw "No organization with slug sample-bank at $ApiUrl; is the sample estate seeded?" }
    return $sample.id
}

function Read-State {
    # Windows PowerShell 5.1 hands a JSON array back as ONE object; the ForEach-Object unrolls it.
    if (Test-Path $stateFile) { return @(Get-Content $stateFile -Raw | ConvertFrom-Json | ForEach-Object { $_ }) }
    return @()
}

switch ($Action) {
    "List" {
        $roster | ForEach-Object {
            "{0,-15} http://localhost:{1}  persona {2,-9} roles {3}" -f $_.Name, $_.Port, $_.Persona, $_.Roles
        }
        "Start with -Action Start. Each UI opens as that user's persona in the Northwind organization."
    }

    "Start" {
        if (-not (Test-Path (Join-Path $repo "ui-next\node_modules\.bin"))) {
            throw "ui-next/node_modules is missing. Restore it first; this script never installs."
        }
        $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
        $orgId = Get-OrgId
        $started = @()
        foreach ($user in Select-Roster) {
            $listener = Get-NetTCPConnection -LocalPort $user.Port -State Listen -ErrorAction SilentlyContinue
            if ($listener) { throw "Port $($user.Port) is already in use; stop it or run -Action Stop." }
            $env:VITE_USE_FIXTURES = "0"
            $env:VITE_AUTH_MODE = "development"
            $env:VITE_DEV_PRINCIPAL_ID = $user.Name
            $env:VITE_DEV_ROLES = $user.Roles
            $env:VITE_DEV_PERSONA = $user.Persona
            $env:VITE_DEV_ORG_ID = $orgId
            $env:VITE_API_PROXY_TARGET = $ApiUrl
            $process = Start-Process -FilePath $npm -PassThru -WindowStyle Hidden -WorkingDirectory $repo `
                -ArgumentList @("--prefix", "ui-next", "run", "dev", "--", "--port", $user.Port, "--strictPort")
            $entry = [pscustomobject]@{ Name = $user.Name; Port = $user.Port; Pid = $process.Id }
            $started += $entry
            @($entry) + (Read-State) | ConvertTo-Json | Set-Content -Path $stateFile -Encoding utf8
            "started {0,-15} http://localhost:{1}" -f $user.Name, $user.Port
        }
        Remove-Item Env:\VITE_USE_FIXTURES, Env:\VITE_AUTH_MODE, Env:\VITE_DEV_PRINCIPAL_ID, Env:\VITE_DEV_ROLES, Env:\VITE_DEV_PERSONA, Env:\VITE_DEV_ORG_ID, Env:\VITE_API_PROXY_TARGET -ErrorAction SilentlyContinue
        "Wait about ten seconds, then run -Action Check."
    }

    "Check" {
        $failed = 0
        $orgId = Get-OrgId
        foreach ($user in Select-Roster) {
            $uri = "http://localhost:$($user.Port)/src/lib/appConfig.ts"
            try {
                $module = (Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 20).Content
            } catch {
                "{0,-15} :{1} NOT RESPONDING" -f $user.Name, $user.Port
                $failed++
                continue
            }
            # Vite inlines import.meta.env into the served module, so this is the identity the
            # browser will send on every request from this origin.
            $serves = $module.Contains('"VITE_DEV_PRINCIPAL_ID": "' + $user.Name + '"') -and
                      $module.Contains('"VITE_DEV_ROLES": "' + $user.Roles + '"') -and
                      $module.Contains('"VITE_DEV_PERSONA": "' + $user.Persona + '"') -and
                      $module.Contains('"VITE_DEV_ORG_ID": "' + $orgId + '"')
            if ($serves) { "{0,-15} :{1} serves its own identity, persona and organization" -f $user.Name, $user.Port }
            else { "{0,-15} :{1} SERVES A DIFFERENT IDENTITY" -f $user.Name, $user.Port; $failed++ }
        }
        if ($failed -gt 0) { exit 1 }
    }

    "Stop" {
        foreach ($entry in Read-State) {
            if ($Users.Count -gt 0 -and $entry.Name -notin $Users) { continue }
            & taskkill.exe /PID $entry.Pid /T /F | Out-Null
            "stopped {0,-15} (pid {1})" -f $entry.Name, $entry.Pid
        }
        # Ports 5181-5188 belong to this roster. Anything still listening on one of them is a
        # server this script started in an earlier run whose record was lost, so free it too.
        foreach ($user in Select-Roster) {
            foreach ($c in @(Get-NetTCPConnection -LocalPort $user.Port -State Listen -ErrorAction SilentlyContinue)) {
                & taskkill.exe /PID $c.OwningProcess /T /F | Out-Null
                "freed   {0,-15} (port {1}, pid {2})" -f $user.Name, $user.Port, $c.OwningProcess
            }
        }
        $remaining = @(Read-State | Where-Object { $Users.Count -gt 0 -and $_.Name -notin $Users })
        if ($remaining.Count -gt 0) { $remaining | ConvertTo-Json | Set-Content -Path $stateFile -Encoding utf8 }
        elseif (Test-Path $stateFile) { Remove-Item $stateFile }
    }
}
