<#
streamthing now-playing agent for Windows.

Reads the system media session (the same one Windows shows in its media flyout:
Spotify, browsers, Media Player, ...) and pushes it to the Car Thing over USB
through `adb forward`. Needs Windows PowerShell 5.1 (powershell.exe), which can
call the WinRT media API directly, and adb.

  powershell -ExecutionPolicy Bypass -File nowplaying.ps1          # run
  powershell -ExecutionPolicy Bypass -File nowplaying.ps1 -Once    # print one snapshot

install.ps1 sets it up to start at logon.
#>
param(
    [string]$Adb = '',
    [int]$Port = 28765,      # host side of the adb forward; the device listens on 8765
    [string]$LogFile = "$env:USERPROFILE\.streamthing\nowplaying.log",
    [switch]$Once
)
$ErrorActionPreference = 'Stop'

function Find-Adb {
    if ($Adb) { return $Adb }
    $cmd = Get-Command adb.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $pkg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter adb.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($pkg) { return $pkg.FullName }
    throw 'adb not found. Install it with: winget install Google.PlatformTools'
}

# --- WinRT plumbing: await IAsyncOperation<T> from PowerShell
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($operation, [Type]$resultType) {
    $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait(5000)) { throw 'WinRT call timed out' }
    $task.Result
}
[void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$SessionManager = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
$MediaProperties = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]
$manager = Await ($SessionManager::RequestAsync()) $SessionManager

function Get-AppName([string]$id) {
    # "Spotify.exe", "Chrome", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify",
    # "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic" -> a readable name
    $name = (($id -split '!')[0] -replace '_[a-z0-9]{13}$', '' -replace '\.exe$', '' -split '\.')[-1]
    switch -regex ($name) {
        '^SpotifyMusic$' { 'Spotify' }
        '^ZuneMusic$'    { 'Media Player' }
        '^msedge$'       { 'Edge' }
        '^chrome$'       { 'Chrome' }
        '^firefox$'      { 'Firefox' }
        '^[0-9A-F]{16}$' { 'Firefox' }     # Firefox registers with a hash
        default          { $name }
    }
}

function Get-NowPlaying {
    $session = $manager.GetCurrentSession()
    if (-not $session) { return $null }
    $props = Await ($session.TryGetMediaPropertiesAsync()) $MediaProperties
    if (-not $props -or -not $props.Title) { return $null }
    $playback = $session.GetPlaybackInfo()
    $timeline = $session.GetTimelineProperties()
    $playing = [string]$playback.PlaybackStatus -eq 'Playing'
    $duration = ($timeline.EndTime - $timeline.StartTime).TotalMilliseconds
    $position = $timeline.Position.TotalMilliseconds
    # Apps update the timeline now and then; extrapolate from when they last did.
    if ($playing -and $timeline.LastUpdatedTime.Year -gt 2000) {
        $position += ([DateTimeOffset]::Now - $timeline.LastUpdatedTime).TotalMilliseconds
    }
    if ($duration -gt 0) { $position = [Math]::Min($position, $duration) }
    [ordered]@{
        title        = $props.Title
        artist       = $props.Artist
        album        = $props.AlbumTitle
        album_artist = $props.AlbumArtist
        track_number = $(if ($props.TrackNumber -gt 0) { $props.TrackNumber } else { $null })
        app          = Get-AppName $session.SourceAppUserModelId
        playing      = $playing
        position_ms  = [int64][Math]::Max(0, $position)
        duration_ms  = $(if ($duration -gt 0) { [int64]$duration } else { $null })
        shuffle      = $playback.IsShuffleActive
        repeat       = $(if ($null -ne $playback.AutoRepeatMode) { [string]$playback.AutoRepeatMode } else { $null })
        thumbnail    = $props.Thumbnail
    }
}

# PowerShell sees the WinRT stream as a bare COM object (no methods, no ContentType), so
# convert it with .NET's AsStreamForRead through reflection, and sniff the image type.
[void][Windows.Storage.Streams.IInputStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$AsStreamForRead = [System.IO.WindowsRuntimeStreamExtensions].GetMethod('AsStreamForRead',
    [Type[]]@([Windows.Storage.Streams.IInputStream]))

function Read-Cover($thumbnail) {
    if (-not $thumbnail) { return $null }
    $winrt = Await ($thumbnail.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
    $stream = $AsStreamForRead.Invoke($null, @($winrt))
    try {
        $buffer = New-Object System.IO.MemoryStream
        $stream.CopyTo($buffer)
        $bytes = $buffer.ToArray()
    } finally {
        $stream.Dispose()
    }
    if ($bytes.Length -eq 0 -or $bytes.Length -gt 3MB) { return $null }
    $type = if ($bytes[0] -eq 0x89 -and $bytes[1] -eq 0x50) { 'image/png' } else { 'image/jpeg' }
    @{ data = [Convert]::ToBase64String($bytes); type = $type }
}

function Write-Log([string]$message) {
    try {
        $dir = Split-Path $LogFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory $dir | Out-Null }
        if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 256KB) { Remove-Item $LogFile }
        Add-Content $LogFile ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $message)
    } catch { }
}

function Get-Hash([string]$text) {
    $md5 = [Security.Cryptography.MD5]::Create()
    -join ($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($text)) | ForEach-Object { $_.ToString('x2') })
}

function Send-Update($body) {
    $json = $body | ConvertTo-Json -Compress -Depth 3
    Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/nowplaying" -Method Post -TimeoutSec 5 `
        -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json))
}

if ($Once) {
    $np = Get-NowPlaying
    if ($np) { $np.Remove('thumbnail'); $np | ConvertTo-Json } else { 'nothing playing' }
    exit
}

$adbExe = Find-Adb
Write-Log "started (adb: $adbExe, port $Port)"
$lastSignature = $null
$lastSent = [DateTime]::MinValue
$coverSentFor = ''      # art key whose cover the device has
$coverTries = 0
$tunnelUp = $false

while ($true) {
    try {
        if (-not $tunnelUp) {
            # Only now call adb: on start and after a failed send (unplugged, rebooted).
            & $adbExe forward "tcp:$Port" tcp:8765 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { Start-Sleep -Seconds 3; continue }
            $tunnelUp = $true
            $lastSignature = $null; $coverSentFor = ''
        }
        $np = Get-NowPlaying
        if ($np) {
            $artKey = Get-Hash ("{0}|{1}|{2}|{3}" -f $np.title, $np.artist, $np.album, $np.app)
            $signature = "{0}|{1}|{2}|{3}" -f $artKey, $np.playing, $np.duration_ms, $np.shuffle
            $interval = if ($np.playing) { 2 } else { 5 }
        } else {
            $artKey = ''
            $signature = 'none'
            $interval = 10
        }
        if ($signature -ne $lastSignature -or ((Get-Date) - $lastSent).TotalSeconds -ge $interval) {
            if ($np) {
                $body = [ordered]@{}
                foreach ($key in $np.Keys) { if ($key -ne 'thumbnail') { $body[$key] = $np[$key] } }
                $body.art_key = $artKey
                if ($artKey -ne $coverSentFor) {
                    if ($signature -ne $lastSignature) { $coverTries = 0 }
                    # Apps often set the cover a moment after the title; retry a few times.
                    if ($coverTries -lt 5) {
                        $coverTries++
                        try { $cover = Read-Cover $np.thumbnail } catch { $cover = $null; Write-Log "cover: $($_.Exception.Message)" }
                        if ($cover) { $body.art = $cover.data; $body.art_type = $cover.type }
                    }
                }
            } else {
                $body = @{ title = $null }
            }
            $reply = Send-Update $body
            if ($body.Contains('art')) { $coverSentFor = $artKey }
            if ($reply.need_art) { $coverSentFor = '' }
            $lastSignature = $signature
            $lastSent = Get-Date
        }
    } catch {
        Write-Log "update failed: $($_.Exception.Message)"
        $tunnelUp = $false      # re-create the forward on the next round
        Start-Sleep -Seconds 2
    }
    Start-Sleep -Milliseconds 1000
}
