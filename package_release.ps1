param(
    [string]$OutZip = "..\LinkedInScraper-release.zip",
    [string]$SourceDir = "."
)

Write-Host "Creating release zip..."

# Files/dirs to exclude
$exclude = @('.git', '.venv', '__pycache__', '*.pyc', '.DS_Store')

# Build a list of files (not strictly required for the Zip API but kept for clarity)
$files = Get-ChildItem -Path $SourceDir -Recurse -File | Where-Object {
    $name = $_.FullName
    foreach ($e in $exclude) { if ($name -like "*$e*") { return $false } }
    return $true
}

# Compute absolute paths (works even if the out file doesn't yet exist)
$sourceFull = [System.IO.Path]::GetFullPath($SourceDir)
$outFull = [System.IO.Path]::GetFullPath($OutZip)

# Ensure output directory exists
$outDir = [System.IO.Path]::GetDirectoryName($outFull)
if (-not [string]::IsNullOrEmpty($outDir)) { [IO.Directory]::CreateDirectory($outDir) | Out-Null }

if (Test-Path $outFull) { Remove-Item $outFull -Force }

Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($sourceFull, $outFull)
Write-Host "Created $outFull"
