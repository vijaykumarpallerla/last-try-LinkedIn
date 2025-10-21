<#
start_tunnel.ps1

Minimal helper that starts ngrok or cloudflared to expose localhost:80 and prints where to look for the public URL.

Usage: .\start_tunnel.ps1
#>

function Write-Err { param($msg) Write-Host $msg -ForegroundColor Red }
function Write-Ok { param($msg) Write-Host $msg -ForegroundColor Green }

Write-Host "Checking prerequisites and looking for ngrok/cloudflared..."

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Err "Warning: Docker not found in PATH. Ensure Docker Desktop is running if you plan to use docker-compose."
}

# Helper to read the first matching URL from a file
function Find-UrlInFile {
    param($filePath)
    if (-not (Test-Path $filePath)) { return $null }
    try {
        $txt = Get-Content $filePath -Raw -ErrorAction Stop
        $m = [regex]::Match($txt, '(https?://[\w\.-:]+)')
        if ($m.Success) { return $m.Groups[1].Value }
    } catch { }
    return $null
}

# Try ngrok
if (Get-Command ngrok -ErrorAction SilentlyContinue) {
    $ngrokPath = (Get-Command ngrok).Source
    Write-Host "Found ngrok at $ngrokPath — starting tunnel to http://localhost:80 (logs -> .\ngrok.out)"
    Start-Process -FilePath $ngrokPath -ArgumentList 'http','80' -NoNewWindow -RedirectStandardOutput .\ngrok.out -WindowStyle Hidden
    Start-Sleep -Seconds 2
    try {
        $api = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -Method GET -ErrorAction Stop
        if ($api.tunnels -and $api.tunnels.Count -gt 0) {
            $public = $api.tunnels[0].public_url
            Write-Ok "ngrok public URL: $public"
            Write-Host "Open $public/vnc/ to access the noVNC client."
            return
        }
    } catch {
        Write-Err "ngrok started but API not reachable yet. Check .\ngrok.out"
    }
}

# Try cloudflared
if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
    $cfPath = (Get-Command cloudflared).Source
    Write-Host "Found cloudflared at $cfPath — starting tunnel to http://localhost:80 (logs -> .\cloudflared.out)"
    Start-Process -FilePath $cfPath -ArgumentList 'tunnel','--url','http://localhost:80' -NoNewWindow -RedirectStandardOutput .\cloudflared.out -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $url = Find-UrlInFile -filePath '.\cloudflared.out'
    if ($url) {
        Write-Ok "cloudflared public URL: $url"
        Write-Host "Open $url/vnc/ to access the noVNC client."
        return
    }
    Write-Err "cloudflared started but public URL not found yet. Check .\cloudflared.out"
}

Write-Err "Neither ngrok nor cloudflared were started. Install one of them or run them manually."
Write-Host "ngrok: https://ngrok.com/download  — run 'ngrok http 80'"
Write-Host "cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/"
