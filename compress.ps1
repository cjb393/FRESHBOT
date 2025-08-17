# Compress any image >10MB in art/ and dnd_maps/ down to <= ~9.5MB, in place.
# Uses ImageMagick "magick". Caches by path+mtime+size in compression_cache.json.
# Windows PowerShell 5 compatible.

$ErrorActionPreference = 'Stop'

# --- folders to scan ---
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dirs = @("$root\art", "$root\dnd_maps")

# --- size thresholds ---
$uploadLimitMB  = 10.0        # your server's cap
$targetMB       = 9.5         # final target (leave ~0.5 MB headroom)
$candidateBytes = [long]($uploadLimitMB * 1024 * 1024)
$targetBytes    = [long]($targetMB      * 1024 * 1024)

# --- cache (path -> "mtime:size") ---
$cachePath = Join-Path $root 'compression_cache.json'
$cache = @{}
if (Test-Path $cachePath) {
  try { $cache = Get-Content $cachePath -Raw | ConvertFrom-Json } catch { $cache = @{} }
}
if ($cache -isnot [hashtable]) {
  $tmp=@{}; $cache.PSObject.Properties | ForEach-Object { $tmp[$_.Name]=$_.Value }; $cache=$tmp
}
function Get-FileTag([System.IO.FileInfo]$fi) { "$($fi.LastWriteTimeUtc.Ticks):$($fi.Length)" }

# --- ensure ImageMagick is available ---
if (-not (Get-Command magick -ErrorAction SilentlyContinue)) {
  Write-Host "ImageMagick 'magick' not found on PATH. Close & reopen the terminal after installing." -ForegroundColor Yellow
  exit 0
}

# --- helpers ---
function Test-IsImageFile {
  param([string]$Path)
  $fmt = & magick identify -ping -format "%m" -- "$Path" 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $fmt) { return $false }
  # Accept common raster formats (add GIF if you want to downconvert animated GIFs to still images)
  return @('PNG','JPEG','JPG','WEBP','BMP','TIFF','TIF').Contains($fmt.ToUpper())
}
function Test-ImageHasAlpha {
  param([string]$Path)
  $ch = & magick identify -ping -format "%[channels]" -- "$Path" 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $ch) { return $false }
  return ($ch -match 'a')  # rgba/srgba/ya
}
function New-TempName {
  param([string]$Suffix)
  $t=[System.IO.Path]::GetTempFileName()
  $n=[System.IO.Path]::ChangeExtension($t,$Suffix.TrimStart('.'))
  if (Test-Path $n) { Remove-Item $n -Force }
  return $n
}
function Optimize-ImageFile {
  param([string]$InPath)

  $hasAlpha = Test-ImageHasAlpha -Path $InPath
  $fmt    = 'jpg';  $suffix = '.jpg'
  if ($hasAlpha) { $fmt = 'webp'; $suffix = '.webp' }

  # Try coarse → fine: scale first, then quality.
  $qualitySteps = 90,85,80,75,70,65,60,55,50,45,40,35,30
  $scaleSteps   = 100,90,80,70,60,55,50,45,40,35

  foreach ($s in $scaleSteps) {
    foreach ($q in $qualitySteps) {
      $out = New-TempName -Suffix $suffix
      try {
        if ($fmt -eq 'jpg') {
          if ($s -lt 100) { & magick "$InPath" -auto-orient -strip -resize "$s%" -sampling-factor 4:2:0 -interlace Plane -quality $q "$out" }
          else            { & magick "$InPath" -auto-orient -strip              -sampling-factor 4:2:0 -interlace Plane -quality $q "$out" }
        } else {
          if ($s -lt 100) { & magick "$InPath" -auto-orient -strip -resize "$s%" -define webp:method=6 -quality $q "$out" }
          else            { & magick "$InPath" -auto-orient -strip              -define webp:method=6 -quality $q "$out" }
        }
      } catch {
        if (Test-Path $out) { Remove-Item $out -Force }
        continue
      }
      if ((Get-Item $out).Length -le $targetBytes) { return $out }
      Remove-Item $out -Force
    }
  }

  # Last resort: clamp smallest side more aggressively for 10MB cap
  $out2 = New-TempName -Suffix $suffix
  try {
    if ($fmt -eq 'jpg') { & magick "$InPath" -auto-orient -strip -resize "1800x1800>" -sampling-factor 4:2:0 -interlace Plane -quality 35 "$out2" }
    else                { & magick "$InPath" -auto-orient -strip -resize "1800x1800>" -define webp:method=6 -quality 50 "$out2" }
  } catch {
    if (Test-Path $out2) { Remove-Item $out2 -Force }
    return $null
  }
  if ((Get-Item $out2).Length -le $targetBytes) { return $out2 }
  Remove-Item $out2 -Force
  return $null
}

# --- process ---
foreach ($dir in $dirs) {
  if (-not (Test-Path $dir)) { continue }

  Write-Host "`nScanning: $dir"
  $all = Get-ChildItem -Path $dir -Recurse -File
  $big = $all | Where-Object { $_.Length -gt $candidateBytes }
  Write-Host ("Files > {0}MB: {1}" -f $uploadLimitMB, $big.Count)

  foreach ($fi in $big) {
    $p = $fi.FullName
    if (-not (Test-IsImageFile -Path $p)) {
      Write-Host "Skip (not an image): $p"
      continue
    }

    $tag = Get-FileTag $fi
    if ($cache.ContainsKey($p) -and $cache[$p] -eq $tag) {
      Write-Host "Skip (cached): $p"
      continue
    }

    Write-Host ("Compressing: {0} ({1:N2} MB)" -f $p, ($fi.Length/1MB))
    $tmp = Optimize-ImageFile -InPath $p
    if (-not $tmp) {
      Write-Host " !! could not compress under $targetMB MB"
      continue
    }

    # move into place (may change extension)
    $newExt = [IO.Path]::GetExtension($tmp)
    $final  = [IO.Path]::ChangeExtension($p, $newExt)
    Move-Item -Force $tmp $final
    if ($final -ne $p -and (Test-Path $p)) { Remove-Item $p -Force }

    $newFi = Get-Item $final
    if ($cache.ContainsKey($p)) { $cache.Remove($p) | Out-Null }
    $cache[$final] = Get-FileTag $newFi
    Write-Host (" -> {0:N2} MB  {1}" -f ($newFi.Length/1MB), $final)
  }
}

# prune & save cache
foreach ($k in @($cache.Keys)) { if (-not (Test-Path $k)) { $cache.Remove($k) | Out-Null } }
($cache | ConvertTo-Json -Depth 4) | Set-Content -Encoding UTF8 -NoNewline $cachePath
Write-Host "`nDone."