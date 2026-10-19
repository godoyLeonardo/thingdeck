<#
streamthing agent for Windows.

Sends the Car Thing the computer's time (for its clock) and lock state, system stats
(CPU, memory, GPU, network), the focused app, the volume and audio devices, the
calendars listed in calendars.txt next to the log, and what is playing in the system
media session (the same one Windows shows in its media flyout: Spotify, browsers,
Media Player, ...), over USB through `adb forward`. It also opens meeting links,
kills the focused app and switches audio devices when the Car Thing asks. Needs Windows PowerShell 5.1
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

# --- the focused app, for the Status page's kill switch. A Store app's window belongs to
# ApplicationFrameHost; the app itself owns one of its child windows.
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class StreamthingFocus {
    delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumProc proc, IntPtr lParam);
    public static string Title = "";
    public static uint Pid() {
        IntPtr window = GetForegroundWindow();
        Title = "";
        if (window == IntPtr.Zero) return 0;
        uint pid;
        GetWindowThreadProcessId(window, out pid);
        var text = new StringBuilder(256);
        GetWindowText(window, text, 256);
        Title = text.ToString();
        try {
            if (System.Diagnostics.Process.GetProcessById((int)pid).ProcessName == "ApplicationFrameHost") {
                uint host = pid;
                EnumChildWindows(window, (child, l) => {
                    uint p;
                    GetWindowThreadProcessId(child, out p);
                    if (p != host) { pid = p; return false; }
                    return true;
                }, IntPtr.Zero);
            }
        } catch (ArgumentException) { }
        return pid;
    }
}
'@
# Never offered for killing: Windows itself, the shell (Explorer also draws the taskbar), and this agent.
$ProtectedNames = @('explorer', 'dwm', 'csrss', 'winlogon', 'wininit', 'lsass', 'services', 'smss', 'svchost', 'system',
    'idle', 'logonui', 'lockapp', 'sihost', 'shellexperiencehost', 'startmenuexperiencehost', 'searchhost',
    'textinputhost', 'applicationframehost', 'streamthing-nowplaying')
$SelfIds = @($PID, (Get-CimInstance Win32_Process -Filter "ProcessId = $PID").ParentProcessId)
$focusApp = @{ pid = 0; app = '' }      # the last focused process's display name, looked up once per process

function Get-Focus {
    $id = [StreamthingFocus]::Pid()
    $proc = if ($id) { Get-Process -Id $id -ErrorAction SilentlyContinue }
    if (-not $proc) { return $null }
    if ($script:focusApp.pid -ne $id) {
        $description = try { $proc.Description } catch { $null }     # the file description: "Google Chrome"
        $script:focusApp = @{ pid = $id; app = $(if ($description) { $description } else { $proc.ProcessName }) }
    }
    [ordered]@{
        pid = [int]$id; name = $proc.ProcessName; app = $script:focusApp.app; title = [StreamthingFocus]::Title
        protected = ($ProtectedNames -contains $proc.ProcessName.ToLower()) -or ($SelfIds -contains [int]$id)
    }
}

# Kill what the Car Thing showed: the same process id and still the same program.
function Stop-Focused($target) {
    $proc = Get-Process -Id ([int]$target.pid) -ErrorAction SilentlyContinue
    $result = @{ name = "$($target.app)"; ok = $false }
    if (-not $proc -or $proc.ProcessName -ne $target.name) { $result.error = 'it is not running anymore'; return $result }
    if ($ProtectedNames -contains $proc.ProcessName.ToLower() -or $SelfIds -contains $proc.Id) { $result.error = 'it is protected'; return $result }
    try {
        Stop-Process -Id $proc.Id -Force -ErrorAction Stop
        Write-Log "killed $($proc.ProcessName) ($($proc.Id))"
        $result.ok = $true
    } catch {
        $result.error = if ($_.Exception.InnerException.NativeErrorCode -eq 5) { 'access denied: it runs as administrator' } else { $_.Exception.Message }
    }
    $result
}
$killResult = $null

# --- audio: the default output's volume and mute, the default output and input, and the
# active devices, through Core Audio; IPolicyConfig (what the Sound settings use) sets a
# new default for every role.
Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace StreamthingAudio {
    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator {
        int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
    }
    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceCollection {
        int GetCount(out int count);
        int Item(int index, out IMMDevice device);
    }
    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice {
        int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
        int OpenPropertyStore(int access, out IPropertyStore store);
        int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        int GetCount(out int count);
        int GetAt(int index, out PropertyKey key);
        int GetValue(ref PropertyKey key, out PropVariant value);
    }
    [ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioEndpointVolume {
        int RegisterControlChangeNotify(IntPtr notify);
        int UnregisterControlChangeNotify(IntPtr notify);
        int GetChannelCount(out int count);
        int SetMasterVolumeLevel(float db, IntPtr context);
        int SetMasterVolumeLevelScalar(float level, IntPtr context);
        int GetMasterVolumeLevel(out float db);
        int GetMasterVolumeLevelScalar(out float level);
        int SetChannelVolumeLevel(uint channel, float db, IntPtr context);
        int SetChannelVolumeLevelScalar(uint channel, float level, IntPtr context);
        int GetChannelVolumeLevel(uint channel, out float db);
        int GetChannelVolumeLevelScalar(uint channel, out float level);
        int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, IntPtr context);
        int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
    }
    // Undocumented but stable since Windows 7: what the Sound settings use to change the default device.
    [ComImport, Guid("F8679F50-850A-41CF-9C72-430F290290C8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPolicyConfig {
        int GetMixFormat(IntPtr a, IntPtr b);
        int GetDeviceFormat(IntPtr a, int b, IntPtr c);
        int ResetDeviceFormat(IntPtr a);
        int SetDeviceFormat(IntPtr a, IntPtr b, IntPtr c);
        int GetProcessingPeriod(IntPtr a, int b, IntPtr c, IntPtr d);
        int SetProcessingPeriod(IntPtr a, IntPtr b);
        int GetShareMode(IntPtr a, IntPtr b);
        int SetShareMode(IntPtr a, IntPtr b);
        int GetPropertyValue(IntPtr a, IntPtr b, IntPtr c);
        int SetPropertyValue(IntPtr a, IntPtr b, IntPtr c);
        int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string id, int role);
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PropertyKey { public Guid FormatId; public int PropertyId; }
    [StructLayout(LayoutKind.Explicit, Size = 24)]     // PROPVARIANT is 24 bytes on 64-bit
    struct PropVariant { [FieldOffset(0)] public short Type; [FieldOffset(8)] public IntPtr Pointer; }

    public static class Audio {
        [DllImport("ole32.dll")] static extern int PropVariantClear(ref PropVariant value);
        const int Render = 0, Capture = 1, Active = 1, ClsCtxAll = 23;

        static IMMDeviceEnumerator Enumerator() {
            return (IMMDeviceEnumerator)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")));
        }
        static string Name(IMMDevice device) {
            IPropertyStore store;
            device.OpenPropertyStore(0, out store);
            var key = new PropertyKey { FormatId = new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), PropertyId = 14 };
            PropVariant value;
            store.GetValue(ref key, out value);
            string name = value.Type == 31 ? Marshal.PtrToStringUni(value.Pointer) : null;     // VT_LPWSTR
            PropVariantClear(ref value);
            return name;
        }
        // The default output's volume (0..100, or -1 without one) and mute.
        public static int Volume(out bool muted) {
            IMMDevice device;
            muted = false;
            if (Enumerator().GetDefaultAudioEndpoint(Render, 1, out device) != 0) return -1;
            var iid = typeof(IAudioEndpointVolume).GUID;
            object o;
            device.Activate(ref iid, ClsCtxAll, IntPtr.Zero, out o);
            var volume = (IAudioEndpointVolume)o;
            float level;
            volume.GetMasterVolumeLevelScalar(out level);
            volume.GetMute(out muted);
            return (int)Math.Round(level * 100);
        }
        // The default device id for "output" or "input" (multimedia role), or null.
        public static string DefaultId(string kind) {
            IMMDevice device;
            if (Enumerator().GetDefaultAudioEndpoint(kind == "input" ? Capture : Render, 1, out device) != 0) return null;
            string id;
            device.GetId(out id);
            return id;
        }
        // Active devices as [id, name] pairs.
        public static string[][] Devices(string kind) {
            IMMDeviceCollection all;
            var list = new List<string[]>();
            if (Enumerator().EnumAudioEndpoints(kind == "input" ? Capture : Render, Active, out all) != 0) return list.ToArray();
            int count;
            all.GetCount(out count);
            for (int i = 0; i < count; i++) {
                IMMDevice device;
                all.Item(i, out device);
                string id;
                device.GetId(out id);
                list.Add(new[] { id, Name(device) ?? id });
            }
            return list.ToArray();
        }
        // Make a device the default for every role (console, multimedia, communications).
        public static void SetDefault(string id) {
            var policy = (IPolicyConfig)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")));
            for (int role = 0; role < 3; role++) Marshal.ThrowExceptionForHR(policy.SetDefaultEndpoint(id, role));
        }
    }
}
'@
$audioListed = [DateTime]::MinValue
$audioLists = $null
$audioSent = $null; $audioListsSent = $null; $audioSentAt = [DateTime]::MinValue

function Get-AudioDevices([string]$kind) {
    foreach ($d in [StreamthingAudio.Audio]::Devices($kind)) { [ordered]@{ id = $d[0]; name = $d[1] } }
}

function Read-Audio {
    if (((Get-Date) - $script:audioListed).TotalSeconds -ge 3) {      # the lists rarely change
        $script:audioListed = Get-Date
        $script:audioLists = [ordered]@{ outputs = @(Get-AudioDevices 'output'); inputs = @(Get-AudioDevices 'input') }
    }
    $muted = $false
    $volume = [StreamthingAudio.Audio]::Volume([ref]$muted)
    [ordered]@{
        volume = $(if ($volume -ge 0) { $volume } else { $null }); muted = $muted
        output = [StreamthingAudio.Audio]::DefaultId('output'); input = [StreamthingAudio.Audio]::DefaultId('input')
    }
}

# Sends the audio state when it changes, and at least every second: the reply carries a
# device the Car Thing picked, so switching takes under a second.
function Update-Audio {
    try { $state = Read-Audio } catch { return }       # no audio devices, or the audio service restarting
    $key = $state | ConvertTo-Json -Compress
    $listsKey = $script:audioLists | ConvertTo-Json -Compress -Depth 4
    if ($key -eq $script:audioSent -and $listsKey -eq $script:audioListsSent -and
        ((Get-Date) - $script:audioSentAt).TotalSeconds -lt 1) { return }
    if ($listsKey -ne $script:audioListsSent) { $state.outputs = $script:audioLists.outputs; $state.inputs = $script:audioLists.inputs }
    $reply = Send-Json 'audio' $state
    $script:audioSent = $key; $script:audioListsSent = $listsKey; $script:audioSentAt = Get-Date
    if ($reply.need_lists) { $script:audioListsSent = $null }
    if ($reply.set) {
        try {
            [StreamthingAudio.Audio]::SetDefault([string]$reply.set.id)
            $script:audioListed = [DateTime]::MinValue
        } catch { Write-Log "audio: could not switch: $($_.Exception.Message)" }
    }
}
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
    [byte[]]$body = if ($calendars) { [Text.Encoding]::UTF8.GetBytes($calendars.ics) } else { @() }   # typed: `if` would unroll it into a list
    [void](Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/calendar$query" -Method Post -TimeoutSec 60 `
        -ContentType 'text/calendar; charset=utf-8' -Body $body)
}

# --- GitHub: a repo's latest workflow runs and your open pull requests, through the gh CLI,
# which keeps the login (nothing is stored here). The repo is the first line of github.txt
# next to the log ("name" for one of yours, or "owner/name").
$GitHubFile = Join-Path (Split-Path $LogFile) 'github.txt'
$ghExe = $null
$ghRepo = @{ name = ''; full = '' }        # the github.txt line and what it resolved to
$PrQuery = '{"query":"query { viewer { pullRequests(states: OPEN, first: 20, orderBy: {field: UPDATED_AT, direction: DESC}) { nodes { number title url isDraft updatedAt reviewDecision repository { nameWithOwner } commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } } } } }"}'

# Run gh and read its output as UTF-8 (PowerShell 5.1 would decode it with the console code page).
function Invoke-Gh([string[]]$arguments, [string]$stdin) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo $script:ghExe
    $psi.Arguments = ($arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = [bool]$stdin
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    $process = [System.Diagnostics.Process]::Start($psi)
    if ($stdin) { $process.StandardInput.Write($stdin); $process.StandardInput.Close() }
    $out = $process.StandardOutput.ReadToEndAsync()
    $err = $process.StandardError.ReadToEnd()
    if (-not $process.WaitForExit(30000)) { $process.Kill(); throw 'gh took too long' }
    if ($process.ExitCode -ne 0) { throw (($err.Trim() -split "`n")[0]) }
    $out.Result
}

function Get-GitHub {
    $name = if (Test-Path $GitHubFile) {
        Get-Content $GitHubFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') } | Select-Object -First 1
    }
    if (-not $name) { return @{ none = $true } }
    if (-not $script:ghExe) { $script:ghExe = (Get-Command gh.exe -ErrorAction SilentlyContinue).Source }
    $result = [ordered]@{ repo = $name; runs = @(); prs = @() }
    if (-not $script:ghExe) { $result.error = 'Install the GitHub CLI (gh) and sign in (see README)'; return $result }
    try {
        if ($script:ghRepo.name -ne $name) {       # "name" alone: one of the signed-in account's repos
            $full = if ($name -match '/') { $name } else { (Invoke-Gh @('repo', 'view', $name, '--json', 'nameWithOwner', '-q', '.nameWithOwner')).Trim() }
            $script:ghRepo = @{ name = $name; full = $full }
        }
        $result.repo = $script:ghRepo.full
        $runs = Invoke-Gh @('run', 'list', '-R', $script:ghRepo.full, '-L', '40', '--json',
            'workflowName,displayTitle,status,conclusion,headBranch,event,createdAt,updatedAt,url') | ConvertFrom-Json
        $seen = @{}         # the newest run of each workflow
        $latest = foreach ($run in $runs) {
            if ($seen.ContainsKey($run.workflowName)) { continue }
            $seen[$run.workflowName] = $true
            [ordered]@{ workflow = $run.workflowName; status = $run.status; conclusion = $run.conclusion; branch = $run.headBranch
                title = $run.displayTitle; event = $run.event; created = $run.createdAt; updated = $run.updatedAt; url = $run.url }
        }
        $result.runs = @($latest | Select-Object -First 5)
        $prs = (Invoke-Gh @('api', 'graphql', '--input', '-') $PrQuery | ConvertFrom-Json).data.viewer.pullRequests.nodes
        $result.prs = @(foreach ($pr in $prs) {
            [ordered]@{ repo = $pr.repository.nameWithOwner; number = $pr.number; title = $pr.title; draft = $pr.isDraft
                review = $pr.reviewDecision; updated = $pr.updatedAt; url = $pr.url
                checks = $(if ($pr.commits.nodes) { $pr.commits.nodes[0].commit.statusCheckRollup.state }) }
        })
    } catch {
        $result.error = $_.Exception.Message
    }
    $result
}
$gitHubFetched = [DateTime]::MinValue
$gitHubBusy = $false        # a run is queued or running: check more often

# Links the Car Thing asks to open: https links of known meeting services, and GitHub.
$OpenableUrl = '^https://(meet\.google\.com|([a-z0-9-]+\.)?zoom\.us|teams\.microsoft\.com|teams\.live\.com|github\.com)/'

if ($Once) {
    $np = Get-NowPlaying
    if ($np) { $np.Remove('thumbnail'); $np | ConvertTo-Json } else { 'nothing playing' }
    Start-Sleep -Seconds 1
    Get-Stats | ConvertTo-Json
    Get-Focus | ConvertTo-Json
    Read-Audio | ConvertTo-Json
    $audioLists | ConvertTo-Json -Depth 4
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
        # Stats (and the focused app) every 2 s; the reply may ask to kill that app, open a
        # meeting link or resend the calendars.
        if (((Get-Date) - $lastStatsSent).TotalSeconds -ge 2) {
            $lastStatsSent = Get-Date
            $stats = Get-Stats
            $stats.focus = Get-Focus
            if ($killResult) { $stats.kill_result = $killResult; $killResult = $null }
            $reply = Send-Json 'stats' $stats
            if ($reply.kill) { $killResult = Stop-Focused $reply.kill; $lastStatsSent = [DateTime]::MinValue }   # report it right away
            if ($reply.open -and $reply.open -match $OpenableUrl) {
                Write-Log "opening $($reply.open -replace '\?.*', '')"
                Start-Process $reply.open
            }
            if ($reply.need_calendar) { $calendarHash = $null }
        }
        # GitHub every minute, or every 15 s while a workflow runs.
        if (((Get-Date) - $gitHubFetched).TotalSeconds -ge $(if ($gitHubBusy) { 15 } else { 60 })) {
            $gitHubFetched = Get-Date
            $gitHub = Get-GitHub
            $gitHubBusy = [bool]($gitHub.runs | Where-Object { $_.status -ne 'completed' })
            [void](Send-Json 'github' $gitHub)
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
    # The volume four times a second between rounds, so the Car Thing's volume bar follows the dial.
    for ($tick = 0; $tick -lt 4; $tick++) {
        if ($tunnelUp) {
            try { Update-Audio } catch { Write-Log "audio update failed: $($_.Exception.Message)"; $tunnelUp = $false }
        }
        Start-Sleep -Milliseconds 250
    }
}
