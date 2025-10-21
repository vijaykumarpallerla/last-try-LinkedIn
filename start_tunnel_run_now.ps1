Set-StrictMode -Version Latest

function Write-Err([string]$m) { Write-Host $m -ForegroundColor Red }
function Write-Ok([string]$m) { Write-Host $m -ForegroundColor Green }

Push-Location -LiteralPath (Split-Path -Path $MyInvocation.MyCommand.Definition -Parent)

function Download-Ngrok {
    if (Test-Path .\ngrok.exe) { return }
    Write-Host "Downloading ngrok..."
    $zip = 'ngrok.zip'
    try {
        Invoke-WebRequest -Uri 'https://bin.equinox.io/c/4VmDzA7iaHb/ngrok-stable-windows-amd64.zip' -OutFile $zip -UseBasicParsing -ErrorAction Stop
        Expand-Archive -Path $zip -DestinationPath .\ngrok_tmp -Force
        Move-Item -Force .\ngrok_tmp\ngrok.exe .\ngrok.exe
        Remove-Item -Recurse -Force .\ngrok_tmp, $zip
        Write-Ok 'ngrok downloaded'
    } catch {
        Write-Err "Failed to download or extract ngrok: $_"
    }
}

function Download-Cloudflared {
    if (Test-Path .\cloudflared.exe) { return }
    Write-Host "Downloading cloudflared..."
    try {
        $url = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
        Invoke-WebRequest -Uri $url -OutFile .\cloudflared.exe -UseBasicParsing -ErrorAction Stop
        Write-Ok 'cloudflared downloaded'
    } catch {
        Write-Err "Failed to download cloudflared: $_"
    }
}

function Start-NgrokAndGetUrl {
    if (-not (Test-Path .\ngrok.exe)) { Write-Err 'ngrok.exe not found'; return $null }
    Get-Process -Name ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host 'Starting ngrok http 80 in background...'
    Start-Process -FilePath .\ngrok.exe -ArgumentList 'http','80' -WindowStyle Hidden -PassThru | Out-Null
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        try {
            $api = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -Method Get -ErrorAction Stop
            if ($api.tunnels -and $api.tunnels.Count -gt 0) { return $api.tunnels[0].public_url }
        } catch { }
    }
    return $null
}

function Start-CloudflaredAndGetUrl {
    if (-not (Test-Path .\cloudflared.exe)) { Write-Err 'cloudflared.exe not found'; return $null }
    Get-Process -Name cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host 'Starting cloudflared tunnel to http://localhost:80 (output -> .\cloudflared.out)'
    $out = (Get-Location).Path + '\\cloudflared.out'
    $err = (Get-Location).Path + '\\cloudflared.err'
    Start-Process -FilePath .\cloudflared.exe -ArgumentList 'tunnel','--url','http://localhost:80' -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru | Out-Null
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        if (Test-Path .\cloudflared.out) {
            try {
                $txt = Get-Content .\cloudflared.out -Raw -ErrorAction Stop
                $m = [regex]::Match($txt, '(https?://[\w\.-]+)')
                if ($m.Success) { return $m.Groups[1].Value }
            } catch { }
        }
    }
    return $null
}

try {
    Write-Host "Working directory: $(Get-Location)"
    Download-Ngrok
    Download-Cloudflared

    $results = @{}

    if (Test-Path .\ngrok.exe) {
        $url = Start-NgrokAndGetUrl
        if ($url) { $results['ngrok'] = $url; Write-Ok "ngrok public URL: $url"; Write-Host "Open $url/vnc/" } else { Write-Err 'ngrok started but no public URL (timeout)' }
    } else { Write-Host 'ngrok not available' }

    if (Test-Path .\cloudflared.exe) {
        $url2 = Start-CloudflaredAndGetUrl
        if ($url2) { $results['cloudflared'] = $url2; Write-Ok "cloudflared public URL: $url2"; Write-Host "Open $url2/vnc/" } else { Write-Err 'cloudflared started but no public URL (timeout)' }
    } else { Write-Host 'cloudflared not available' }

    if ($results.Count -eq 0) { Write-Err 'No tunnels created.' } else { Write-Host 'Summary:'; $results.GetEnumerator() | ForEach-Object { Write-Host "$_" } }
} catch { Write-Err "Failed: $_" } finally { Pop-Location }
