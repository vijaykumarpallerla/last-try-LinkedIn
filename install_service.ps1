param(
    [ValidateSet('install','remove')][string]$Action = 'install',
    [string]$ServiceName = 'LinkedInScraper',
    [string]$PythonExe = 'python',
    [string]$AppDir = (Get-Location).Path
)

function Get-NssmPath {
    # try common locations
    $candidates = @(
        "$env:ProgramFiles\nssm\nssm.exe",
        "$env:ProgramFiles(x86)\nssm\nssm.exe",
        "$env:LOCALAPPDATA\nssm\nssm.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

$nssm = Get-NssmPath
if ($Action -eq 'install') {
    if ($nssm) {
        Write-Host "Installing service via NSSM at $nssm"
        & $nssm install $ServiceName $PythonExe "-c \"from waitress import serve; import wsgi; serve(wsgi.app, listen='127.0.0.1:5001')\"" 
        & $nssm start $ServiceName
        Write-Host "Service $ServiceName installed and started."
    } else {
        Write-Host "NSSM not found. Falling back to scheduled task example. To install NSSM, download from https://nssm.cc/ and place nssm.exe in Program Files or LocalAppData."
        $schtask = "schtasks /Create /SC ONSTART /TN \"$ServiceName\" /TR \"$PythonExe -c `"from waitress import serve; import wsgi; serve(wsgi.app, listen='127.0.0.1:5001')`\" /RL HIGHEST /F"
        Write-Host "Run this (elevated) to register a scheduled task that runs at startup:`n$schtask"
    }
} else {
    if ($nssm) {
        & $nssm stop $ServiceName
        & $nssm remove $ServiceName confirm
        Write-Host "Service removed via NSSM"
    } else {
        Write-Host "Remove scheduled task (if created): schtasks /Delete /TN \"$ServiceName\" /F"
    }
}
