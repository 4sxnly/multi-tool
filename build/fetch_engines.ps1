# Downloads the native engines the app ships in bin\ (run at BUILD time, never on user PCs).
#   pwsh -File build/fetch_engines.ps1
# Every file is checked against the publisher's SHA-256 list before it's accepted.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest much faster

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$bin = Join-Path $root "bin"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "multitool-engines"
New-Item -ItemType Directory -Force -Path $bin, $tmp | Out-Null

function Get-File([string]$url, [string]$out) {
    Write-Host "  downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
}

# Checksum files come in different shapes and encodings (UTF-8, UTF-16 from PowerShell,
# "hash  filename" lists, or "Hash : ..." blocks). Normalise to plain text first.
function ConvertTo-PlainText($content) {
    if ($content -is [byte[]]) {
        $bytes = $content
        if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
            $text = [System.Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)
        } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) {
            $text = [System.Text.Encoding]::BigEndianUnicode.GetString($bytes, 2, $bytes.Length - 2)
        } else {
            $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        }
    } else {
        $text = [string]$content
    }
    return $text.Replace([string][char]0, "")
}

function Assert-Sha256([string]$file, [string]$sumsUrl, [string]$assetName, [switch]$Optional) {
    try {
        $raw = (Invoke-WebRequest -Uri $sumsUrl -UseBasicParsing).Content
    } catch {
        if ($Optional) { Write-Warning "No checksum list at $sumsUrl - skipping verification of $assetName"; return }
        throw
    }
    $text = ConvertTo-PlainText $raw
    $hashPattern = "(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"
    $all = @([regex]::Matches($text, $hashPattern) | ForEach-Object { $_.Value.ToLower() } | Select-Object -Unique)

    $expected = $null
    if ($all.Count -eq 1) {
        # single-asset checksum file (any layout)
        $expected = $all[0]
    } elseif ($all.Count -gt 1) {
        # multi-asset list: take the hash on the line that names this asset
        $line = ($text -split "\r?\n") |
            Where-Object { $_ -match ([regex]::Escape($assetName) + "\s*$") } |
            Select-Object -First 1
        if ($line) { $expected = [regex]::Match($line, $hashPattern).Value.ToLower() }
    }
    if (-not $expected) { throw "No checksum for $assetName in $sumsUrl" }

    $actual = (Get-FileHash -Algorithm SHA256 -Path $file).Hash.ToLower()
    if ($actual -ne $expected) { throw "Checksum mismatch for $assetName (expected $expected, got $actual)" }
    Write-Host "  sha256 OK  $assetName"
}

# ── yt-dlp (official exe already contains the yt-dlp-ejs challenge scripts) ──
Write-Host "yt-dlp"
$yt = Join-Path $tmp "yt-dlp.exe"
Get-File "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" $yt
Assert-Sha256 $yt "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS" "yt-dlp.exe"
Copy-Item $yt (Join-Path $bin "yt-dlp.exe") -Force

# ── Deno (JavaScript runtime yt-dlp needs for YouTube) ──
Write-Host "deno"
$denoAsset = "deno-x86_64-pc-windows-msvc.zip"
$denoZip = Join-Path $tmp $denoAsset
Get-File "https://github.com/denoland/deno/releases/latest/download/$denoAsset" $denoZip
Assert-Sha256 $denoZip "https://github.com/denoland/deno/releases/latest/download/$denoAsset.sha256sum" $denoAsset -Optional
Expand-Archive -Path $denoZip -DestinationPath (Join-Path $tmp "deno") -Force
Copy-Item (Join-Path $tmp "deno\deno.exe") (Join-Path $bin "deno.exe") -Force

# ── FFmpeg (shared build: ffmpeg + ffprobe share one set of DLLs) ──
Write-Host "ffmpeg"
$ffAsset = "ffmpeg-master-latest-win64-gpl-shared.zip"
$ffZip = Join-Path $tmp $ffAsset
Get-File "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/$ffAsset" $ffZip
Assert-Sha256 $ffZip "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256" $ffAsset -Optional
$ffDir = Join-Path $tmp "ffmpeg"
Expand-Archive -Path $ffZip -DestinationPath $ffDir -Force
$ffBin = Get-ChildItem -Path $ffDir -Recurse -Directory -Filter "bin" | Select-Object -First 1
Copy-Item (Join-Path $ffBin.FullName "ffmpeg.exe"), (Join-Path $ffBin.FullName "ffprobe.exe") $bin -Force
Copy-Item (Join-Path $ffBin.FullName "*.dll") $bin -Force
$license = Get-ChildItem -Path $ffDir -Recurse -File -Filter "LICENSE*" | Select-Object -First 1
if ($license) { Copy-Item $license.FullName (Join-Path $bin "FFMPEG-LICENSE.txt") -Force }

# ── llama.cpp server: offline AI for the Desktop Companion ──
# Two official builds: Vulkan (uses almost any graphics card) and CPU (works everywhere).
Write-Host "llama.cpp"
$ghHeaders = @{ "User-Agent" = "multitool-build";
