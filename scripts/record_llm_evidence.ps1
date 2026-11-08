# One-step, secure recording of ONE genuine OpenAI run (up to 6 paid calls, hard-capped) -> public_data\llm_evidence.json
#   powershell -ExecutionPolicy Bypass -File .\scripts\record_llm_evidence.ps1
# Security properties:
#  * The key is read from a hidden prompt (or reused from SENTINEL_LLM_API_KEY if already set in your shell). It is never written to
#    disk, never printed, never put on a command line, and only lives in THIS PowerShell process's environment during the run.
#  * Every environment variable this script touches is restored to its previous value afterwards (also on failure / Ctrl+C).
#  * Any SENTINEL_LLM_BASE_URL in your shell is ignored: the recorder uses the official OpenAI endpoint, and the evidence file records
#    that host; a run against any other endpoint cannot produce a valid evidence file.
$ErrorActionPreference = 'Stop'
$names = 'SENTINEL_LLM_API_KEY','SENTINEL_LLM_BASE_URL','SENTINEL_LLM_ENABLE','SENTINEL_LLM_PROVIDER','SENTINEL_LLM_MODEL','SENTINEL_LLM_MAX_CALLS','SENTINEL_PUBLIC'
$orig = @{}
foreach ($n in $names) { $orig[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
$code = 1
try {
    Set-Location (Split-Path -Parent $PSScriptRoot)
    if (-not $env:SENTINEL_LLM_API_KEY) {
        $sec = Read-Host 'OpenAI API key (hidden, not saved)' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { $env:SENTINEL_LLM_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    if (-not $env:SENTINEL_LLM_API_KEY) { throw 'No key entered; nothing was run.' }
    Remove-Item Env:SENTINEL_PUBLIC -ErrorAction SilentlyContinue
    Remove-Item Env:SENTINEL_LLM_BASE_URL -ErrorAction SilentlyContinue
    $env:SENTINEL_LLM_ENABLE = '1'
    $env:SENTINEL_LLM_PROVIDER = 'openai'
    $env:SENTINEL_LLM_MODEL = 'gpt-4.1-mini'
    $env:SENTINEL_LLM_MAX_CALLS = '6'
    python (Join-Path 'scripts' 'record_llm_evidence.py')
    if ($LASTEXITCODE -ne 0) { Write-Host "RECORDING FAILED (exit $LASTEXITCODE). Nothing was written."; $code = $LASTEXITCODE }
    else {
        python (Join-Path 'scripts' 'verify_llm_evidence.py') (Join-Path 'public_data' 'llm_evidence.json')
        if ($LASTEXITCODE -ne 0) { Write-Host 'VERIFY FAILED'; $code = $LASTEXITCODE }
        else {
            $code = 0
            Write-Host ''
            Write-Host 'DONE. Tell Claude "recording finished"; Claude will verify the file and rebuild the public package.'
        }
    }
}
catch { Write-Host ("STOPPED: " + $_.Exception.Message); $code = 1 }
finally {
    foreach ($n in $names) { [Environment]::SetEnvironmentVariable($n, $orig[$n], 'Process') }
}
exit $code
