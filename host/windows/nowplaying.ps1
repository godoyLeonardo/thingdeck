<#
streamthing agent for Windows.

Sends the Car Thing the computer's time (for its clock) and lock state, system stats
(CPU, memory, GPU, network), the calendars listed in calendars.txt next to the log,
and what is playing in the system media session (the same one Windows shows in its
media flyout: Spotify, browsers, Media Player, ...), over USB through `adb forward`.
It also opens meeting links the Car Thing asks for. Needs Windows PowerShell 5.1
(powershell.exe), which can call the WinRT media API directly, and adb.

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

function Send-Json([string]$path, $body) {
    $json = $body | ConvertTo-Json -Compress -Depth 3
    Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/$path" -Method Post -TimeoutSec 5 `
        -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json))
}

# --- system stats: CPU and memory straight from kernel32 (performance counter names are
# translated on non-English Windows), the GPU from nvidia-smi when there is one, and the
# network from the adapters that have a default gateway (the ones that reach the internet).
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class StreamthingStats {
    [StructLayout(LayoutKind.Sequential)]
    public class MemoryStatus {
        public uint Length = 64; public uint Load; public ulong TotalPhys; public ulong AvailPhys;
        public ulong TotalPageFile; public ulong AvailPageFile; public ulong TotalVirtual; public ulong AvailVirtual;
        public ulong AvailExtendedVirtual;
    }
    [DllImport("kernel32.dll")] static extern bool GetSystemTimes(out long idle, out long kernel, out long user);
    [DllImport("kernel32.dll")] static extern bool GlobalMemoryStatusEx([In, Out] MemoryStatus status);
    static long lastIdle, lastTotal;
    // Busy share of all cores since the last call (kernel time includes idle time).
    public static double Cpu() {
        long idle, kernel, user;
        GetSystemTimes(out idle, out kernel, out user);
        long total = kernel + user, dt = total - lastTotal, di = idle - lastIdle;
        bool first = lastTotal == 0;
        lastIdle = idle; lastTotal = total;
        return first || dt <= 0 ? -1 : 100.0 * (dt - di) / dt;
    }
    public static ulong[] Memory() {
        var m = new MemoryStatus();
        GlobalMemoryStatusEx(m);
        return new ulong[] { m.TotalPhys, m.TotalPhys - m.AvailPhys };
    }
}
'@
[void][StreamthingStats]::Cpu()
$nvidiaSmi = (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue).Source
$lastNet = $null

function Get-Stats {
    $cpu = [StreamthingStats]::Cpu()
    $mem = [StreamthingStats]::Memory()
    $stats = [ordered]@{
        cpu = $(if ($cpu -ge 0) { [Math]::Round($cpu, 1) } else { $null })
        mem_total_mb = [int64]($mem[0] / 1MB)
        mem_used_mb = [int64]($mem[1] / 1MB)
    }
    if ($nvidiaSmi) {
        $line = & $nvidiaSmi --query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total `
            --format=csv,noheader,nounits 2>$null
        $f = "$(@($line)[0])" -split ',\s*'     # first GPU
        if ($LASTEXITCODE -eq 0 -and $f.Count -eq 4) {
            $stats.gpu = [double]$f[0]; $stats.gpu_temp = [double]$f[1]
            $stats.gpu_mem_used_mb = [int64]$f[2]; $stats.gpu_mem_total_mb = [int64]$f[3]
        }
    }
    $rx = 0; $tx = 0
    foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($nic.OperationalStatus -ne 'Up' -or $nic.GetIPProperties().GatewayAddresses.Count -eq 0) { continue }
        $io = $nic.GetIPStatistics()
        $rx += $io.BytesReceived; $tx += $io.BytesSent
    }
    $now = [DateTime]::UtcNow
    if ($script:lastNet) {
        $seconds = ($now - $script:lastNet.time).TotalSeconds
        if ($seconds -gt 0) {
            $stats.net_rx = [int64][Math]::Max(0, ($rx - $script:lastNet.rx) / $seconds)    # bytes/s
            $stats.net_tx = [int64][Math]::Max(0, ($tx - $script:lastNet.tx) / $seconds)
        }
    }
    $script:lastNet = @{ time = $now; rx = $rx; tx = $tx }
    $stats
}

# --- calendars: each line of calendars.txt is an iCal address, such as Google Calendar's
# "Secret address in iCal format". The Car Thing reads the files itself (repeats, time
# zones); this only downloads them and passes them on when they change.
$CalendarFile = Join-Path (Split-Path $LogFile) 'calendars.txt'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Get-Calendars {
    if (-not (Test-Path $CalendarFile)) { return $null }
    $ics = New-Object System.Text.StringBuilder
    $errors = @()
    foreach ($url in Get-Content $CalendarFile) {
        $url = $url.Trim() -replace '^webcal://', 'https://'
        if (-not $url -or $url.StartsWith('#')) { continue }
        try {
            $client = New-Object System.Net.WebClient
            $client.Encoding = [Text.Encoding]::UTF8
            [void]$ics.AppendLine($client.DownloadString($url))
        } catch {
            $errors += ($_.Exception.Message -replace '[\r\n]+', ' ')
        }
    }
    @{ ics = $ics.ToString(); error = ($errors -join '; ') }
}

function Send-Calendars($calendars) {
    $query = if (-not $calendars) { '?none=1' } elseif ($calendars.error) { '?error=' + [Uri]::EscapeDataString($calendars.error) } else { '' }
    $body = if ($calendars) { [Text.Encoding]::UTF8.GetBytes($calendars.ics) } else { [byte[]]@() }
    [void](Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/calendar$query" -Method Post -TimeoutSec 60 `
        -ContentType 'text/calendar; charset=utf-8' -Body $body)
}

# The Car Thing asks to open a meeting link; only https links of known meeting services.
$MeetingUrl = '^https://(meet\.google\.com|([a-z0-9-]+\.)?zoom\.us|teams\.microsoft\.com|teams\.live\.com)/'

if ($Once) {
    $np = Get-NowPlaying
    if ($np) { $np.Remove('thumbnail'); $np | ConvertTo-Json } else { 'nothing playing' }
    Start-Sleep -Seconds 1
    Get-Stats | ConvertTo-Json
    exit
}

$adbExe = Find-Adb
Write-Log "started (adb: $adbExe, port $Port)"
$lastSignature = $null
$lastSent = [DateTime]::MinValue
$coverSentFor = ''      # art key whose cover the device has
$coverTries = 0
$tunnelUp = $false
$lastHost = $null
$lastHostSent = [DateTime]::MinValue
$lastStatsSent = [DateTime]::MinValue
$calendarHash = $null        # what the device has
$calendarFetched = [DateTime]::MinValue
$calendars = $null
$fetchedHash = $null

while ($true) {
    try {
        if (-not $tunnelUp) {
            # Only now call adb: on start and after a failed send (unplugged, rebooted).
            & $adbExe forward "tcp:$Port" tcp:8765 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { Start-Sleep -Seconds 3; continue }
            $tunnelUp = $true
            $lastSignature = $null; $coverSentFor = ''; $lastHost = $null; $calendarHash = $null
        }
        # Stats every 2 s; the reply may ask to open a meeting link or to resend the calendars.
        if (((Get-Date) - $lastStatsSent).TotalSeconds -ge 2) {
            $lastStatsSent = Get-Date
            $reply = Send-Json 'stats' (Get-Stats)
            if ($reply.open -and $reply.open -match $MeetingUrl) {
                Write-Log "opening $($reply.open -replace '\?.*', '')"
                Start-Process $reply.open
            }
            if ($reply.need_calendar) { $calendarHash = $null }
        }
        if (((Get-Date) - $calendarFetched).TotalMinutes -ge 5) {
            $calendarFetched = Get-Date
            $calendars = Get-Calendars
            if ($calendars -and $calendars.error) { Write-Log "calendar: $($calendars.error)" }
            $fetchedHash = if ($calendars) { Get-Hash ($calendars.ics + '|' + $calendars.error) } else { 'none' }
        }
        if ($fetchedHash -ne $calendarHash) {
            Send-Calendars $calendars
            $calendarHash = $fetchedHash
        }
        # Heartbeat: the time and UTC offset set the Car Thing's clock; LogonUI runs only
        # while the lock (or sign-in) screen is up.
        $locked = [bool](Get-Process LogonUI -ErrorAction SilentlyContinue)
        $offset = [int][TimeZoneInfo]::Local.GetUtcOffset([DateTime]::Now).TotalMinutes
        if ("$locked|$offset" -ne $lastHost -or ((Get-Date) - $lastHostSent).TotalSeconds -ge 10) {
            [void](Send-Json 'host' @{
                time_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                utc_offset_min = $offset
                locked = $locked
            })
            $lastHost = "$locked|$offset"
            $lastHostSent = Get-Date
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
            $reply = Send-Json 'nowplaying' $body
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
