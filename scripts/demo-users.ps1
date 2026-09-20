<#
.SYNOPSIS
  Run one Atlas UI per demo user, each with that user's own development identity.

.DESCRIPTION
  The shipped UI bakes ONE development identity into its bundle (`local-ui-admin` holding every
  role), so the container on :3001 cannot show two different users. This starts a Vite dev server
  per demo user instead. Each server proxies /v1 to the running API and sends that user's
  `X-Principal-Id` / `X-Roles`, so the BACKEND decides what the user may do -- nothing is faked
  in the browser. Separate ports are separate browser origins, so each tab also keeps its own
  organization and persona selection.

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
       Roles = "PlatformAdmin,OrganizationAdmin,MetadataAdmin,DataAdmin,SemanticAdmin,DataSteward,Reviewer,MetadataReviewer,Auditor,Operations,Analyst,Viewer,ToolDeveloper,ToolConsumer,AgentDeveloper" },
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

function Get-OrgSnippet {
    # A user without Auditor/Operations/OrganizationAdmin/PlatformAdmin cannot list organizations,
    # so the shell cannot offer a picker; it remembers the choice in localStorage per origin.
    $headers = @{ "X-Principal-Id" = "demo-launcher"; "X-Roles" = "PlatformAdmin" }
    $orgs = (Invoke-RestMethod -Uri "$ApiUrl/v1/organizations" -Headers $headers).items
    $sample = $orgs | Where-Object { $_.slug -eq "sample-bank" } | Select-Object -First 1
    if ($null -eq $sample) { return "No sample-bank organization found; pick one in the shell." }
    return "localStorage.setItem('atlas.org.id','$($sample.id)');location.reload()"
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
        "Start with -Action Start. In each tab choose the persona above; then pick the Northwind"
        "organization (the selector, or -- for users who cannot list organizations -- this one-liner"
        "in the browser console):"
        "  " + (Get-OrgSnippet)
    }

    "Start" {
        if (-not (Test-Path (Join-Path $repo "ui-next\node_modules\.bin"))) {
            throw "ui-next/node_modules is missing. Restore it first; this script never installs."
        }
        $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
        $started = @()
        foreach ($user in Select-Roster) {
            $listener = Get-NetTCPConnection -LocalPort $user.Port -State Listen -ErrorAction SilentlyContinue
            if ($listener) { throw "Port $($user.Port) is already in use; stop it or run -Action Stop." }
            $env:VITE_USE_FIXTURES = "0"
            $env:VITE_AUTH_MODE = "development"
            $env:VITE_DEV_PRINCIPAL_ID = $user.Name
            $env:VITE_DEV_ROLES = $user.Roles
            $env:VITE_API_PROXY_TARGET = $ApiUrl
            $process = Start-Process -FilePath $npm -PassThru -WindowStyle Hidden -WorkingDirectory $repo `
                -ArgumentList @("--prefix", "ui-next", "run", "dev", "--", "--port", $user.Port, "--strictPort")
            $started += [pscustomobject]@{ Name = $user.Name; Port = $user.Port; Pid = $process.Id }
            "started {0,-15} http://localhost:{1}" -f $user.Name, $user.Port
        }
        Remove-Item Env:\VITE_USE_FIXTURES, Env:\VITE_AUTH_MODE, Env:\VITE_DEV_PRINCIPAL_ID, Env:\VITE_DEV_ROLES, Env:\VITE_API_PROXY_TARGET -ErrorAction SilentlyContinue
        $started + (Read-State) | ConvertTo-Json | Set-Content -Path $stateFile -Encoding utf8
        "Wait a few seconds, then run -Action Check. Console one-liner for a user who cannot list"
        "organizations (Viewer, Reviewer): " + (Get-OrgSnippet)
    }

    "Check" {
        $failed = 0
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
                      $module.Contains('"VITE_DEV_ROLES": "' + $user.Roles + '"')
            if ($serves) { "{0,-15} :{1} serves its own identity" -f $user.Name, $user.Port }
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
        $remaining = @(Read-State | Where-Object { $Users.Count -gt 0 -and $_.Name -notin $Users })
        if ($remaining.Count -gt 0) { $remaining | ConvertTo-Json | Set-Content -Path $stateFile -Encoding utf8 }
        elseif (Test-Path $stateFile) { Remove-Item $stateFile }
    }
}
