<#
  SENTINEL - create the encrypted credential file for the US sandbox profile.

  What it does
    * Prompts for the Client ID and Client Secret of the app SENTINEL-US-SANDBOX with HIDDEN input
      (Read-Host -AsSecureString). Nothing is echoed, logged or passed on a command line.
    * Saves them ONLY as DPAPI-encrypted SecureStrings (Export-Clixml) in
      paypal-sandbox-credentials-us.xml in the project folder. DPAPI means only YOUR Windows user on THIS
      computer can decrypt it. The file is git-ignored (*.xml).
    * Never touches the India file (paypal-sandbox-credentials.xml).
    * Refuses to overwrite an existing US file unless you pass -Force.

  Run it yourself in a normal PowerShell window (not while screen-sharing, not through chat):
      cd C:\Users\kavit\Roadworthy
      powershell -ExecutionPolicy Bypass -File .\scripts\set-us-credentials.ps1
#>
param([switch]$Force)
$ErrorActionPreference = 'Stop'

$root   = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root 'paypal-sandbox-credentials-us.xml'
$tmp    = Join-Path $root ('.paypal-us-credentials-' + [guid]::NewGuid().ToString('N') + '.tmp')

if ((Test-Path -LiteralPath $target) -and -not $Force) {
    Write-Host 'The US credential file already exists. Nothing was changed. Use -Force only if you mean to replace it.'
    exit 1
}

Write-Host 'Paste each value when prompted. Input is hidden. Do not share these values in chat.'
$id  = Read-Host -Prompt 'SENTINEL-US-SANDBOX Client ID' -AsSecureString
$sec = Read-Host -Prompt 'SENTINEL-US-SANDBOX Client Secret' -AsSecureString

if ($id.Length -lt 10 -or $sec.Length -lt 10) {
    Write-Host 'One of the values looks too short. Nothing was saved.'
    exit 1
}

try {
    [pscustomobject]@{ ClientId = $id; ClientSecret = $sec } | Export-Clixml -LiteralPath $tmp
    $check = Import-Clixml -LiteralPath $tmp
    if (-not ($check.ClientId -is [System.Security.SecureString]) -or -not ($check.ClientSecret -is [System.Security.SecureString])) {
        throw 'Saved file is not in the expected encrypted form.'
    }
    Move-Item -LiteralPath $tmp -Destination $target -Force
    Write-Host 'Saved: paypal-sandbox-credentials-us.xml (encrypted for your Windows user on this computer).'
    Write-Host 'Next: python -m roadworthy profile-check --profile us --expect-merchant-id <Merchant ID>'
}
finally {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    $id.Dispose(); $sec.Dispose()
    Remove-Variable id, sec, check -ErrorAction SilentlyContinue
    Set-Clipboard -Value ' '
    Write-Host 'Clipboard cleared.'
}
