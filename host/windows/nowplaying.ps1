<#
thingdeck agent for Windows.

Sends the Car Thing the computer's time (for its clock) and lock state, system stats
(CPU, memory, GPU, network), the focused app, the volume and audio devices, the
calendars listed in calendars.txt next to the log, the Plex server in plex.txt and the TrueNAS
server in truenas.txt next to
the log, and what is playing in the system
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
    [string]$LogFile = "$env:USERPROFILE\.thingdeck\nowplaying.log",
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
        if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 256KB) {     # keep one older file
            Move-Item $LogFile "$LogFile.1" -Force
        }
        Add-Content $LogFile ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $message)
    } catch { }
}

function Get-Hash([string]$text) {
    $md5 = [Security.Cryptography.MD5]::Create()
    -join ($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($text)) | ForEach-Object { $_.ToString('x2') })
}

function Send-Json([string]$path, $body, [int]$depth = 3) {     # rooms with lights and scenes need more depth
    $json = $body | ConvertTo-Json -Compress -Depth $depth
    Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/$path" -Method Post -TimeoutSec 5 `
        -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json))
}

# --- system stats: CPU and memory straight from kernel32 (performance counter names are
# translated on non-English Windows), the GPU from nvidia-smi when there is one, and the
# network from the adapters that have a default gateway (the ones that reach the internet).
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ThingdeckStats {
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
[void][ThingdeckStats]::Cpu()

# --- the focused app, for the Status page's kill switch. A Store app's window belongs to
# ApplicationFrameHost; the app itself owns one of its child windows.
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class ThingdeckFocus {
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
    'textinputhost', 'applicationframehost', 'thingdeck-agent')
$SelfIds = @($PID, (Get-CimInstance Win32_Process -Filter "ProcessId = $PID").ParentProcessId)
$focusApp = @{ pid = 0; app = '' }      # the last focused process's display name, looked up once per process

function Get-Focus {
    $id = [ThingdeckFocus]::Pid()
    $proc = if ($id) { Get-Process -Id $id -ErrorAction SilentlyContinue }
    if (-not $proc) { return $null }
    if ($script:focusApp.pid -ne $id) {
        $description = try { $proc.Description } catch { $null }     # the file description: "Google Chrome"
        $script:focusApp = @{ pid = $id; app = $(if ($description) { $description } else { $proc.ProcessName }) }
    }
    [ordered]@{
        pid = [int]$id; name = $proc.ProcessName; app = $script:focusApp.app; title = [ThingdeckFocus]::Title
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
using System.Text;

namespace ThingdeckAudio {
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
    // Per-app volume: the default output's audio sessions, one or more per app.
    [ComImport, Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionManager2 {
        int GetAudioSessionControl(IntPtr groupingParam, int flags, out IntPtr control);       // IAudioSessionManager
        int GetSimpleAudioVolume(IntPtr groupingParam, int flags, out IntPtr volume);
        int GetSessionEnumerator(out IAudioSessionEnumerator sessions);
    }
    [ComImport, Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionEnumerator {
        int GetCount(out int count);
        int GetSession(int index, out IAudioSessionControl2 session);
    }
    [ComImport, Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioSessionControl2 {
        int GetState(out int state);                                                          // IAudioSessionControl
        int GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string name);
        int SetDisplayName([MarshalAs(UnmanagedType.LPWStr)] string name, IntPtr context);
        int GetIconPath([MarshalAs(UnmanagedType.LPWStr)] out string path);
        int SetIconPath([MarshalAs(UnmanagedType.LPWStr)] string path, IntPtr context);
        int GetGroupingParam(out Guid param);
        int SetGroupingParam(ref Guid param, IntPtr context);
        int RegisterAudioSessionNotification(IntPtr events);
        int UnregisterAudioSessionNotification(IntPtr events);
        int GetSessionIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string id);           // IAudioSessionControl2
        int GetSessionInstanceIdentifier([MarshalAs(UnmanagedType.LPWStr)] out string id);
        int GetProcessId(out uint pid);
        [PreserveSig] int IsSystemSoundsSession();
    }
    [ComImport, Guid("87CE5498-68D6-44E5-9215-6DA47EF883D8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface ISimpleAudioVolume {
        int SetMasterVolume(float level, IntPtr context);
        int GetMasterVolume(out float level);
        int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, IntPtr context);
        int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
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
        // Set the default output's volume (0..100, or -1 to keep it) and mute (-1 keep, 0 off, 1 on).
        public static void SetMaster(int level, int mute) {
            IMMDevice device;
            if (Enumerator().GetDefaultAudioEndpoint(Render, 1, out device) != 0) return;
            var iid = typeof(IAudioEndpointVolume).GUID;
            object o;
            device.Activate(ref iid, ClsCtxAll, IntPtr.Zero, out o);
            var volume = (IAudioEndpointVolume)o;
            if (level >= 0) volume.SetMasterVolumeLevelScalar(Math.Min(100, level) / 100f, IntPtr.Zero);
            if (mute >= 0) volume.SetMute(mute == 1, IntPtr.Zero);
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

        // The default output's sessions that haven't ended: (process id, system sounds, active, volume, mute).
        public class Session { public uint Pid; public bool System, Active, Muted; public float Volume; internal ISimpleAudioVolume Control; }
        static List<Session> ReadSessions() {
            var list = new List<Session>();
            IMMDevice device;
            if (Enumerator().GetDefaultAudioEndpoint(Render, 1, out device) != 0) return list;
            var iid = typeof(IAudioSessionManager2).GUID;
            object o;
            if (device.Activate(ref iid, ClsCtxAll, IntPtr.Zero, out o) != 0) return list;
            IAudioSessionEnumerator sessions;
            if (((IAudioSessionManager2)o).GetSessionEnumerator(out sessions) != 0) return list;
            int count;
            sessions.GetCount(out count);
            for (int i = 0; i < count; i++) {
                IAudioSessionControl2 control;
                if (sessions.GetSession(i, out control) != 0) continue;
                int state;
                control.GetState(out state);
                if (state == 2) continue;                              // expired
                var session = new Session { Active = state == 1, System = control.IsSystemSoundsSession() == 0 };
                control.GetProcessId(out session.Pid);
                session.Control = (ISimpleAudioVolume)control;
                session.Control.GetMasterVolume(out session.Volume);
                session.Control.GetMute(out session.Muted);
                list.Add(session);
            }
            return list;
        }
        public static Session[] Sessions() { return ReadSessions().ToArray(); }

        // A process's program file with only "limited query" access, which Windows allows for
        // more processes (games, other users' apps) than Process.Path, which reads its memory.
        [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(int access, bool inherit, uint pid);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder name, ref int size);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        public static string ProcessPath(uint pid) {
            IntPtr process = OpenProcess(0x1000, false, pid);        // PROCESS_QUERY_LIMITED_INFORMATION
            if (process == IntPtr.Zero) return null;
            try {
                var name = new StringBuilder(1024);
                int size = name.Capacity;
                return QueryFullProcessImageName(process, 0, name, ref size) ? name.ToString() : null;
            } finally { CloseHandle(process); }
        }
        // Set the volume (0..100, or -1 to keep it) and the mute (-1 keep, 0 off, 1 on) of the sessions of these processes.
        public static void SetSessions(uint[] pids, int volume, int mute) {
            foreach (var s in ReadSessions()) {
                if (Array.IndexOf(pids, s.Pid) < 0) continue;
                if (volume >= 0) s.Control.SetMasterVolume(Math.Min(100, volume) / 100f, IntPtr.Zero);
                if (mute >= 0) s.Control.SetMute(mute == 1, IntPtr.Zero);
            }
        }
    }
}
'@
$audioListed = [DateTime]::MinValue
$audioLists = $null
$audioSent = $null; $audioListsSent = $null; $audioSentAt = [DateTime]::MinValue

function Get-AudioDevices([string]$kind) {
    foreach ($d in [ThingdeckAudio.Audio]::Devices($kind)) { [ordered]@{ id = $d[0]; name = $d[1] } }
}

function Read-Audio {
    if (((Get-Date) - $script:audioListed).TotalSeconds -ge 3) {      # the lists rarely change
        $script:audioListed = Get-Date
        $script:audioLists = [ordered]@{ outputs = @(Get-AudioDevices 'output'); inputs = @(Get-AudioDevices 'input') }
    }
    $muted = $false
    $volume = [ThingdeckAudio.Audio]::Volume([ref]$muted)
    [ordered]@{
        volume = $(if ($volume -ge 0) { $volume } else { $null }); muted = $muted
        output = [ThingdeckAudio.Audio]::DefaultId('output'); input = [ThingdeckAudio.Audio]::DefaultId('input')
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
            [ThingdeckAudio.Audio]::SetDefault([string]$reply.set.id)
            $script:audioListed = [DateTime]::MinValue
        } catch { Write-Log "audio: could not switch: $($_.Exception.Message)" }
    }
}
# --- Per-app volume, like the Volume Mixer: the default output's sessions grouped by app.
# The Car Thing gets each app's name, volume, mute and whether it plays, and its icon once.
# It sends back absolute volumes and mutes through the command poll (see Update-Hue).
Add-Type -AssemblyName System.Drawing
$mixerProcs = @{}          # process id -> @{ key; name; path } (looked up once per process)
$mixerPids = @{}           # app key -> its process ids, for the commands
$mixerSent = $null; $mixerSentAt = [DateTime]::MinValue; $mixerIcons = @()

function Get-MixerProc([uint32]$id) {
    if ($script:mixerProcs.ContainsKey($id)) { return $script:mixerProcs[$id] }
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    $info = if (-not $p) { @{ key = "pid$id"; name = "Process $id"; path = $null } } else {
        $path = $p.Path        # null for elevated and protected processes: then ask for less
        if (-not $path) { $path = [ThingdeckAudio.Audio]::ProcessPath($id) }
        $desc = if ($path) { (Get-Item -LiteralPath $path -ErrorAction SilentlyContinue).VersionInfo.FileDescription } else { $null }
        @{ key = $p.ProcessName.ToLower(); name = $(if ($desc -and $desc.Trim()) { $desc.Trim() } else { $p.ProcessName }); path = $path
            # The icon's id: the program file it comes from, so the Car Thing knows when it has the right one.
            icon = $(if ($path) { (Get-Hash $path).Substring(0, 8) } else { $null }) }
    }
    $script:mixerProcs[$id] = $info
    $info
}

function Read-Mixer {
    $apps = [ordered]@{}
    $pids = @{}
    foreach ($s in [ThingdeckAudio.Audio]::Sessions()) {
        $proc = if ($s.System) { @{ key = 'system'; name = 'System sounds'; path = $null } } else { Get-MixerProc $s.Pid }
        if (-not $apps.Contains($proc.key)) {
            $apps[$proc.key] = [ordered]@{ key = $proc.key; name = $proc.name; volume = 0; muted = $true; active = $false; icon_id = $proc.icon }
            $pids[$proc.key] = @()
        }
        $app = $apps[$proc.key]
        $app.volume = [Math]::Max($app.volume, [int][Math]::Round($s.Volume * 100))     # the loudest of its sessions
        $app.muted = $app.muted -and $s.Muted
        $app.active = $app.active -or $s.Active
        $pids[$proc.key] += $s.Pid
    }
    $script:mixerPids = $pids
    # Playing apps first, then by name; system sounds last.
    @($apps.Values | Sort-Object @{ e = { $_.key -eq 'system' } }, @{ e = { -not $_.active } }, @{ e = { $_.name } })
}

# A 48 px PNG of the app's icon, from its program file.
function Get-MixerIcon([string]$key) {
    $proc = $script:mixerProcs.Values | Where-Object { $_.key -eq $key -and $_.path } | Select-Object -First 1
    if (-not $proc) { return $null }
    $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($proc.path)
    $bitmap = New-Object System.Drawing.Bitmap 48, 48
    $g = [System.Drawing.Graphics]::FromImage($bitmap)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.DrawImage($icon.ToBitmap(), 0, 0, 48, 48)
    $g.Dispose()
    $memory = New-Object System.IO.MemoryStream
    $bitmap.Save($memory, [System.Drawing.Imaging.ImageFormat]::Png)
    [Convert]::ToBase64String($memory.ToArray())
}

# Every second (called more often; it waits on its own): sends the apps when they change, at
# least every 5 s, with the icons the Car Thing asked for.
function Update-Mixer {
    if (((Get-Date) - $script:mixerSentAt).TotalSeconds -lt 1) { return }
    try { $apps = Read-Mixer } catch { return }
    $key = $apps | ConvertTo-Json -Compress
    if ($key -eq $script:mixerSent -and -not $script:mixerIcons.Count -and ((Get-Date) - $script:mixerSentAt).TotalSeconds -lt 5) { return }
    $body = [ordered]@{ apps = $apps }
    if ($script:mixerIcons.Count) {
        $icons = [ordered]@{}
        foreach ($k in $script:mixerIcons) { try { $png = Get-MixerIcon $k; if ($png) { $icons[$k] = $png } } catch { } }
        $body.icons = $icons
    }
    $reply = Send-Json 'mixer' $body 4
    $script:mixerSent = $key; $script:mixerSentAt = Get-Date
    $script:mixerIcons = @($reply.need_icons | Where-Object { $_ })
}

# Commands from the Car Thing: @{ key; volume? (0..100); mute? (bool) }; the key "*" is All, the
# default output itself.
function Invoke-MixerCommand($command) {
    $volume = if ($null -ne $command.volume) { [int]$command.volume } else { -1 }
    $mute = if ($null -ne $command.mute) { [int][bool]$command.mute } else { -1 }
    if ([string]$command.key -eq '*') {
        [ThingdeckAudio.Audio]::SetMaster($volume, $mute)
        $script:audioSentAt = [DateTime]::MinValue    # the audio update shows it
        return
    }
    $pids = [uint32[]]@($script:mixerPids[[string]$command.key])
    if (-not $pids.Count) { return }
    [ThingdeckAudio.Audio]::SetSessions($pids, $volume, $mute)
    $script:mixerSentAt = [DateTime]::MinValue        # show the result right away
}

$nvidiaSmi = (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue).Source
$gpuAt = [DateTime]::MinValue; $gpu = $null

# The live panels on the Car Thing's screen ("lights", "monitor", "github", "plex", …), from
# its command poll. Those update often; the others slowly, which saves the computer the work.
$visible = @()
function Test-Visible([string]$widget) { $script:visible -contains $widget }
# A panel just came on screen: read its data now instead of at its next turn.
function Show-Panel([string]$widget) {
    switch ($widget) {
        'lights' { $script:hueFetched = [DateTime]::MinValue }
        'github' { $script:gitHubFetched = [DateTime]::MinValue }
        'plex' { $script:plexNext = [DateTime]::MinValue }
        'truenas' { $script:nasNext = [DateTime]::MinValue }
        'monitor' { $script:gpuAt = [DateTime]::MinValue }
        # The calendars come from Google and others: not more than twice a minute.
        'agenda' { if (((Get-Date) - $script:calendarFetched).TotalSeconds -ge 30) { $script:calendarFetched = [DateTime]::MinValue } }
    }
}
$lastNet = $null

function Get-Stats {
    $cpu = [ThingdeckStats]::Cpu()
    $mem = [ThingdeckStats]::Memory()
    $stats = [ordered]@{
        cpu = $(if ($cpu -ge 0) { [Math]::Round($cpu, 1) } else { $null })
        mem_total_mb = [int64]($mem[0] / 1MB)
        mem_used_mb = [int64]($mem[1] / 1MB)
    }
    # nvidia-smi is the costly part: every 2 s while Status is on screen, else every 30 s.
    if ($nvidiaSmi -and ((Get-Date) - $script:gpuAt).TotalSeconds -ge $(if (Test-Visible 'monitor') { 0 } else { 30 })) {
        $script:gpuAt = Get-Date
        $line = & $nvidiaSmi --query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total `
            --format=csv,noheader,nounits 2>$null
        $f = "$(@($line)[0])" -split ',\s*'     # first GPU
        $script:gpu = if ($LASTEXITCODE -eq 0 -and $f.Count -eq 4) {
            @{ gpu = [double]$f[0]; gpu_temp = [double]$f[1]; gpu_mem_used_mb = [int64]$f[2]; gpu_mem_total_mb = [int64]$f[3] }
        }
    }
    if ($script:gpu) { foreach ($k in $script:gpu.Keys) { $stats[$k] = $script:gpu[$k] } }
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
$ghWorkflows = @{}                         # workflow id (as text: Int32 and Int64 keys differ) -> name
$ghWorkflowsAt = [DateTime]::MinValue
# One request for the last 100 runs (gh run list makes a second one for the workflow names).
$RunFields = '[.workflow_runs[] | {id, workflow_id, name, display_title, status, conclusion, head_branch, event, created_at, updated_at, html_url}]'
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

# A run's own "name" is its run-name, which a workflow can set per run, so the names come
# from the workflow list: read once an hour, or when a run has a workflow not seen yet (each
# new id is looked up only once; a deleted workflow keeps its run's name).
function Update-WorkflowNames($runs) {
    $unknown = @($runs | Where-Object { -not $script:ghWorkflows.ContainsKey([string]$_.workflow_id) })
    if (-not $unknown -and ((Get-Date) - $script:ghWorkflowsAt).TotalHours -lt 1) { return }
    $script:ghWorkflows = @{}
    $list = Invoke-Gh @('api', "repos/$($script:ghRepo.full)/actions/workflows?per_page=100", '--jq', '[.workflows[] | {id, name}]') | ConvertFrom-Json
    foreach ($w in $list) { $script:ghWorkflows[[string]$w.id] = $w.name }
    $script:ghWorkflowsAt = Get-Date
    foreach ($run in $runs) {
        if (-not $script:ghWorkflows.ContainsKey([string]$run.workflow_id)) { $script:ghWorkflows[[string]$run.workflow_id] = $run.name }
    }
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
            $script:ghWorkflows = @{}
            $script:ghWorkflowsAt = [DateTime]::MinValue
        }
        $result.repo = $script:ghRepo.full
        $runs = Invoke-Gh @('api', "repos/$($script:ghRepo.full)/actions/runs?per_page=100&exclude_pull_requests=true",
            '--jq', $RunFields) | ConvertFrom-Json
        Update-WorkflowNames $runs
        $seen = @{}         # the newest run of each workflow
        $latest = foreach ($run in $runs) {
            $id = [string]$run.workflow_id
            if ($seen.ContainsKey($id)) { continue }
            $seen[$id] = $true
            [ordered]@{ workflow = $script:ghWorkflows[$id]; status = $run.status; conclusion = "$($run.conclusion)"; branch = $run.head_branch
                title = $run.display_title; event = $run.event; created = $run.created_at; updated = $run.updated_at; url = $run.html_url
                run_id = $run.id }
        }
        $result.runs = @($latest | Select-Object -First 20)
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

# The details a card asks for when it opens on the Car Thing (through the command poll): for a
# run, its number, attempt, actor, commit and jobs (each with its step counts, the step that
# runs and the step that failed); for a pull request, its branches, size, merge state,
# reviews and checks. gh takes a second or two; the loop waits for it.
$RunDetailFields = '{number: .run_number, attempt: .run_attempt, actor: .actor.login, sha: .head_sha, commit: ((.head_commit.message // "") | split("\n")[0]), started: .run_started_at, status, conclusion, updated: .updated_at}'
$JobFields = '[.jobs[] | {name, status, conclusion, started: .started_at, completed: .completed_at, steps: [.steps[]? | {name, status, conclusion}]}]'
$PrDetailJson = 'state,isDraft,author,baseRefName,headRefName,additions,deletions,changedFiles,commits,comments,mergeable,mergeStateStatus,reviewDecision,labels,createdAt,latestReviews,reviewRequests,statusCheckRollup'
$PrDetailFields = '{state, draft: .isDraft, author: .author.login, base: .baseRefName, head: .headRefName, additions, deletions, files: .changedFiles, commits: (.commits | length), comments: (.comments | length), mergeable, merge_state: .mergeStateStatus, review: .reviewDecision, labels: [.labels[].name], created: .createdAt, reviews: [.latestReviews[] | {by: .author.login, state}], requested: [.reviewRequests[] | (.login // .name // .slug // "")], checks: [.statusCheckRollup[] | {name: (.name // .context // ""), workflow: (.workflowName // ""), status: (.status // ""), conclusion: (.conclusion // .state // ""), started: (.startedAt // ""), completed: (.completedAt // "")}]}'
function Send-GitHubDetails($ask) {
    $out = [ordered]@{ row = $ask.row; seq = $ask.seq; kind = $ask.kind }
    try {
        if (-not $script:ghExe) { $script:ghExe = (Get-Command gh.exe -ErrorAction SilentlyContinue).Source }
        if (-not $script:ghExe) { throw 'Install the GitHub CLI (gh) and sign in (see README)' }
        if ($ask.kind -eq 'run') {
            $path = "repos/$($ask.repo)/actions/runs/$([long]$ask.run_id)"
            $run = Invoke-Gh @('api', $path, '--jq', $RunDetailFields) | ConvertFrom-Json
            foreach ($p in $run.PSObject.Properties) { $out[$p.Name] = $p.Value }
            $jobs = Invoke-Gh @('api', "$path/jobs?per_page=100", '--jq', $JobFields) | ConvertFrom-Json
            $out.jobs = @(foreach ($j in $jobs) {
                $steps = @($j.steps)
                [ordered]@{ name = $j.name; status = $j.status; conclusion = $j.conclusion; started = $j.started; completed = $j.completed
                    steps = $steps.Count; done = @($steps | Where-Object { $_.status -eq 'completed' }).Count
                    current = (@($steps | Where-Object { $_.status -eq 'in_progress' }) | Select-Object -First 1).name
                    failed = (@($steps | Where-Object { $_.conclusion -eq 'failure' }) | Select-Object -First 1).name }
            })
        } elseif ($ask.kind -eq 'pr') {
            $pr = Invoke-Gh @('pr', 'view', [string][int]$ask.number, '--repo', $ask.repo, '--json', $PrDetailJson, '--jq', $PrDetailFields) | ConvertFrom-Json
            foreach ($p in $pr.PSObject.Properties) { $out[$p.Name] = $p.Value }
        }
    } catch { $out.error = $_.Exception.Message }
    [void](Send-Json 'github/details' $out 6)
}

# --- Philips Hue. The Car Thing has no network, so the agent talks to the Hue Bridge (its
# local API v2) and passes rooms, zones, lights and scenes on. The bridge's address and key
# are in hue.json next to the log, written when you pair from the Car Thing's Lights page.
$HueFile = Join-Path (Split-Path $LogFile) 'hue.json'
Add-Type @'
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class ThingdeckHueTls {
    public static string Bridge = "", Nas = "";
    // The bridge's certificate comes from Philips' own authority, not a public one, and a
    // TrueNAS server's is usually self-signed: accept them for those two addresses only;
    // everything else is checked as usual.
    static bool Check(object sender, X509Certificate certificate, X509Chain chain, SslPolicyErrors errors) {
        if (errors == SslPolicyErrors.None) return true;
        var request = sender as HttpWebRequest;
        if (request == null) return false;
        string host = request.RequestUri.Host;
        return (Bridge != "" && host == Bridge) || (Nas != "" && host == Nas);
    }
    public static void Install() { ServicePointManager.ServerCertificateValidationCallback = Check; }
}
'@
[ThingdeckHueTls]::Install()
$hue = $null                 # @{ bridge; key } from hue.json
$huePairing = $null          # @{ until; next } while waiting for the bridge's button
$hueFetched = [DateTime]::MinValue; $hueListed = [DateTime]::MinValue; $hueLists = $null
$hueSent = $null; $hueSentAt = [DateTime]::MinValue

function Read-HueConfig {
    if (-not (Test-Path $HueFile)) { return $null }
    $config = Get-Content $HueFile -Raw | ConvertFrom-Json
    if (-not $config.bridge -or -not $config.key) { return $null }
    [ThingdeckHueTls]::Bridge = $config.bridge
    @{ bridge = [string]$config.bridge; key = [string]$config.key }
}

# One request to the bridge, read as UTF-8 (room names can have accents).
function Invoke-Hue([string]$method, [string]$path, $body, [string]$bridge = $script:hue.bridge, [string]$key = $script:hue.key) {
    $request = [System.Net.HttpWebRequest]::Create("https://$bridge$path")
    $request.Method = $method
    $request.Timeout = 5000
    if ($key) { $request.Headers.Add('hue-application-key', $key) }
    if ($null -ne $body) {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress -Depth 6))
        $request.ContentType = 'application/json'
        $request.ContentLength = $bytes.Length
        $stream = $request.GetRequestStream(); $stream.Write($bytes, 0, $bytes.Length); $stream.Close()
    }
    try { $response = $request.GetResponse() }
    catch [System.Net.WebException] { if ($_.Exception.Response) { $response = $_.Exception.Response } else { throw } }
    $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [Text.Encoding]::UTF8)
    try { $text = $reader.ReadToEnd() } finally { $reader.Close(); $response.Close() }
    $result = $text | ConvertFrom-Json
    if ($result.errors -and @($result.errors).Count) { throw ('Hue: ' + (@($result.errors)[0].description)) }
    $result
}

# Pairing: find the bridge, then ask for a key every 2 s for 30 s while you press its button.
function Step-HuePairing {
    $p = $script:huePairing
    if ((Get-Date) -lt $p.next) { return }
    if ((Get-Date) -gt $p.until) {
        $script:huePairing = $null
        Send-HueState @{ state = 'error'; error = 'The bridge''s button wasn''t pressed. Tap Pair, then press it within 30 seconds.' }
        return
    }
    $p.next = (Get-Date).AddSeconds(2)
    if (-not $p.bridge) {
        $found = @(Invoke-RestMethod -Uri 'https://discovery.meethue.com/' -TimeoutSec 10) | Where-Object { $_.internalipaddress } | Select-Object -First 1
        if (-not $found) { throw 'No Hue Bridge found on the network' }
        $p.bridge = $found.internalipaddress
        [ThingdeckHueTls]::Bridge = $p.bridge
    }
    $answer = @(Invoke-Hue 'POST' '/api' @{ devicetype = "thingdeck#$env:COMPUTERNAME"; generateclientkey = $true } $p.bridge '')[0]
    if ($answer.success.username) {
        New-Item -ItemType Directory -Force (Split-Path $HueFile) | Out-Null
        @{ bridge = $p.bridge; key = $answer.success.username } | ConvertTo-Json | Set-Content $HueFile -Encoding UTF8
        Write-Log "hue: paired with the bridge at $($p.bridge)"
        $script:huePairing = $null
        $script:hue = Read-HueConfig
        $script:hueFetched = [DateTime]::MinValue; $script:hueListed = [DateTime]::MinValue
    }
}

function Send-HueState($state) {
    [void](Send-Json 'hue' $state 8)
    $script:hueSent = $state | ConvertTo-Json -Compress -Depth 8
    $script:hueSentAt = Get-Date
}

# Rooms and zones with their lights and scenes. The structure (rooms, devices) is read every
# 30 s; the light levels and which scene is active every second while Lights is on screen (30 s
# otherwise), and 0.7 s after a command.
function Read-Hue {
    if (-not $script:hueLists -or ((Get-Date) - $script:hueListed).TotalSeconds -ge 30) {
        $script:hueLists = @{
            rooms = @((Invoke-Hue 'GET' '/clip/v2/resource/room').data) + @((Invoke-Hue 'GET' '/clip/v2/resource/zone').data)
            devices = @((Invoke-Hue 'GET' '/clip/v2/resource/device').data)
        }
        $script:hueListed = Get-Date
    }
    $script:hueLists.scenes = @((Invoke-Hue 'GET' '/clip/v2/resource/scene').data)      # their "active" changes with the lights
    $lights = @{}; foreach ($l in (Invoke-Hue 'GET' '/clip/v2/resource/light').data) { $lights[$l.id] = $l }
    $groups = @{}; foreach ($g in (Invoke-Hue 'GET' '/clip/v2/resource/grouped_light').data) { $groups[$g.id] = $g }
    $deviceLights = @{}
    foreach ($d in $script:hueLists.devices) { $deviceLights[$d.id] = @($d.services | Where-Object { $_.rtype -eq 'light' } | ForEach-Object { $_.rid }) }
    $rooms = foreach ($r in $script:hueLists.rooms) {
        $groupId = @($r.services | Where-Object { $_.rtype -eq 'grouped_light' } | ForEach-Object { $_.rid })[0]
        $ids = foreach ($c in $r.children) { if ($c.rtype -eq 'light') { $c.rid } elseif ($c.rtype -eq 'device') { $deviceLights[$c.rid] } }
        $group = if ($groupId) { $groups[$groupId] }
        [ordered]@{
            id = $r.id; kind = $r.type; name = $r.metadata.name; archetype = $r.metadata.archetype; group = $groupId
            on = [bool]$group.on.on
            brightness = $(if ($group.dimming) { [Math]::Round($group.dimming.brightness, 1) } else { $null })
            lights = @(foreach ($id in $ids) {
                $l = $lights[$id]
                if (-not $l) { continue }
                [ordered]@{
                    id = $l.id; name = $l.metadata.name; on = [bool]$l.on.on
                    brightness = $(if ($l.dimming) { [Math]::Round($l.dimming.brightness, 1) } else { $null })
                    xy = $(if ($l.color) { @($l.color.xy.x, $l.color.xy.y) } else { $null })
                    mirek = $(if ($l.color_temperature.mirek_valid) { $l.color_temperature.mirek } else { $null })
                    ct = [bool]$l.color_temperature
                }
            })
            scenes = @(foreach ($s in $script:hueLists.scenes) {
                if ($s.group.rid -eq $r.id) { [ordered]@{ id = $s.id; name = $s.metadata.name; active = ($s.status.active -and $s.status.active -ne 'inactive') } }
            })
        }
    }
    @{ state = 'ok'; rooms = @($rooms | Sort-Object { $_.kind -ne 'room' }, { $_.name }) }
}

function Invoke-HueCommand($cmd) {
    switch ($cmd.op) {
        'pair' { $script:huePairing = @{ until = (Get-Date).AddSeconds(30); next = [DateTime]::MinValue; bridge = $null }
                 Send-HueState @{ state = 'pairing' }; return }
        'scene' { [void](Invoke-Hue 'PUT' "/clip/v2/resource/scene/$($cmd.id)" @{ recall = @{ action = 'active' } }) }
        'all_off' { foreach ($r in $script:hueLists.rooms) {
                        $g = @($r.services | Where-Object { $_.rtype -eq 'grouped_light' })[0]
                        if ($g -and $r.type -eq 'room') { [void](Invoke-Hue 'PUT' "/clip/v2/resource/grouped_light/$($g.rid)" @{ on = @{ on = $false } }) } } }
        { $_ -in 'group', 'light' } {
            $body = @{}
            if ($null -ne $cmd.on) { $body.on = @{ on = [bool]$cmd.on } }
            if ($null -ne $cmd.brightness) { $body.dimming = @{ brightness = [Math]::Max(1, [double]$cmd.brightness) } }
            $kind = if ($cmd.op -eq 'group') { 'grouped_light' } else { 'light' }
            if ($body.Count) { [void](Invoke-Hue 'PUT' "/clip/v2/resource/$kind/$($cmd.id)" $body) }
            if ($null -ne $cmd.mirek) {           # white temperature: per light, on the lights that have it
                $room = $script:hueLists.rooms | Where-Object { @($_.services | Where-Object { $_.rid -eq $cmd.id }).Count } | Select-Object -First 1
                $last = (Read-Hue).rooms | Where-Object { $_.id -eq $room.id } | Select-Object -First 1
                foreach ($l in $last.lights) { if ($l.ct) { [void](Invoke-Hue 'PUT' "/clip/v2/resource/light/$($l.id)" @{ color_temperature = @{ mirek = [int]$cmd.mirek } }) } }
            }
        }
    }
    # Read it back soon, but not at once: the bridge reports a change a moment late.
    $script:hueFetched = (Get-Date).AddSeconds(0.7 - $(if (Test-Visible 'lights') { 1 } else { 30 }))
}

# Several times a second: commands from the Car Thing, pairing, and the lights' state.
function Update-Hue {
    $polled = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/commands" -TimeoutSec 5
    $commands = $polled.hue
    if ($polled.github_refresh) { $script:gitHubFetched = [DateTime]::MinValue }     # the refresh button on the Code page
    if ($polled.github_details) { Send-GitHubDetails $polled.github_details }          # a card opened on the Code page
    foreach ($c in @($polled.mixer)) { if ($c) { try { Invoke-MixerCommand $c } catch { Write-Log "mixer: $($_.Exception.Message)" } } }
    $script:calendarSoon = [bool]$polled.calendar_soon                                  # an event starts within 15 minutes
    $was = $script:visible
    $script:visible = @($polled.visible | Where-Object { $_ })
    foreach ($w in $script:visible) { if ($was -notcontains $w) { Show-Panel $w } }
    if (-not $script:hue -and -not $script:huePairing) { $script:hue = Read-HueConfig }
    foreach ($cmd in @($commands)) {
        if (-not $cmd) { continue }
        try {
            if ($cmd.op -ne 'pair' -and -not $script:hue) { throw 'Pair the Hue Bridge first' }
            Invoke-HueCommand $cmd
        } catch { Write-Log "hue: $($_.Exception.Message)"; Send-HueState @{ state = 'error'; error = $_.Exception.Message } }
    }
    if ($script:huePairing) {
        try { Step-HuePairing } catch { Write-Log "hue pairing: $($_.Exception.Message)"; $script:huePairing = $null
            Send-HueState @{ state = 'error'; error = $_.Exception.Message } }
        return
    }
    if (((Get-Date) - $script:hueFetched).TotalSeconds -lt $(if (Test-Visible 'lights') { 1 } else { 30 })) { return }
    $script:hueFetched = Get-Date
    $state = if (-not $script:hue) { @{ state = 'none' } } else {
        try { Read-Hue } catch {
            $script:hueFetched = (Get-Date).AddSeconds(28)      # bridge unreachable: retry in 30 s, don't stall the loop
            @{ state = 'error'; error = $_.Exception.Message }
        }
    }
    $key = $state | ConvertTo-Json -Compress -Depth 8
    if ($key -ne $script:hueSent -or ((Get-Date) - $script:hueSentAt).TotalSeconds -ge 10) { Send-HueState $state }
}

# --- Plex. A Plex Media Server on the local network: its streams, load, bandwidth and
# libraries. The first line of plex.txt next to the log is the server's address
# (192.168.1.20, or http://192.168.1.20:32400); a second line holds a Plex token, for a
# server that asks for one on the local network.
$PlexFile = Join-Path (Split-Path $LogFile) 'plex.txt'
$PlexStep = 6           # seconds per sample in the server's own load and bandwidth history
$Dot = " $([char]0x00B7) "; $Dash = " $([char]0x2014) "      # this file stays ASCII: Windows PowerShell reads it as ANSI
# Plex items can have keys that differ only in case ("guid" and "Guid"), which ConvertFrom-Json
# refuses in Windows PowerShell; this reader keeps them apart.
Add-Type -AssemblyName System.Web.Extensions
$PlexJson = New-Object System.Web.Script.Serialization.JavaScriptSerializer
$PlexJson.MaxJsonLength = [int]::MaxValue
$plex = @{ server = $null; token = $null }
$plexNext = [DateTime]::MinValue                     # when to read the server next
$plexCounted = [DateTime]::MinValue; $plexLibraries = @(); $plexUpdate = $null
$plexThumbs = @{}; $plexNeed = @()                   # poster key -> @{ path; square }; keys the Car Thing asked for
$plexSent = $null; $plexSentAt = [DateTime]::MinValue; $plexError = $null

function Read-PlexConfig {
    $lines = @()
    if (Test-Path $PlexFile) { $lines = @(Get-Content $PlexFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') }) }
    $server = if ($lines.Count) { $lines[0].TrimEnd('/') } else { $null }
    if ($server -and $server -notmatch '://') { $server = "http://$server" }
    if ($server -and $server -notmatch ':\d+$') { $server += ':32400' }
    if ($server -ne $script:plex.server) { $script:plexCounted = [DateTime]::MinValue; $script:plexLibraries = @() }
    $script:plex = @{ server = $server; token = $(if ($lines.Count -gt 1) { $lines[1] } else { $null }) }
}

# One request to the server: its MediaContainer, or the bytes with -Raw. With -Optional, an
# HTTP error (a Plex Pass part on a server without it) gives $null instead of an error.
function Invoke-Plex([string]$path, [switch]$Raw, [switch]$Optional) {
    $request = [System.Net.HttpWebRequest]::Create($script:plex.server + $path)
    $request.Accept = 'application/json'
    $request.Timeout = 4000
    $request.ReadWriteTimeout = 4000
    if ($script:plex.token) { $request.Headers.Add('X-Plex-Token', $script:plex.token) }
    try { $response = $request.GetResponse() }
    catch [System.Net.WebException] { if ($_.Exception.Response) { $response = $_.Exception.Response } else { throw } }
    try {
        $status = [int]$response.StatusCode
        if ($status -ge 400) {
            if ($Optional) { return $null }
            if ($status -eq 401 -or $status -eq 403) { throw 'The server asks for a Plex token: put it on the second line of plex.txt' }
            throw "HTTP $status for $path"
        }
        $memory = New-Object System.IO.MemoryStream
        $response.GetResponseStream().CopyTo($memory)
    } finally { $response.Close() }
    if ($Raw) { return ,$memory.ToArray() }
    $container = $script:PlexJson.DeserializeObject([Text.Encoding]::UTF8.GetString($memory.ToArray()))['MediaContainer']
    if ($container) { $container } else { @{} }
}

# One stream from /status/sessions, as the Car Thing shows it, plus its poster's path.
# What a stream's card shows: the item, the player and its network, the media, the streams picked
# (with Plex's decision for each) and, for a transcode, what changes and how fast.
$PlexStreams = @{ 1 = 'video'; 2 = 'audio'; 3 = 'subtitles' }
function Get-PlexDetails($s, $player, $media, $transcode) {
    $part = if ($media -and $media.Part) { @($media.Part)[0] } else { $null }
    $streams = @(foreach ($st in @($part.Stream)) {
        if (-not $st) { continue }
        $type = [int]$st.streamType
        if (-not $PlexStreams.ContainsKey($type)) { continue }
        $picked = if ($st.ContainsKey('selected')) { [bool]$st.selected } else { $type -eq 1 }
        if ($picked) { [ordered]@{ kind = $PlexStreams[$type]; title = $(if ($st.displayTitle) { $st.displayTitle } else { $st.codec }); decision = $st.decision } }
    }) | Select-Object -First 6
    [ordered]@{
        library = $s.librarySectionTitle; year = $s.year; rating = $s.contentRating
        product = $player.product; platform = $player.platform; device = $(if ($player.device) { $player.device } else { $player.model })
        address = $player.address; relayed = [bool]$player.relayed; secure = $player.secure
        container = $media.container; video = $media.videoCodec; resolution = $media.videoResolution; fps = $media.videoFrameRate
        audio = $media.audioCodec; channels = $media.audioChannels; bitrate = $media.bitrate; streams = @($streams)
        transcode = $(if (-not $transcode) { $null } else { [ordered]@{
            video = $transcode.videoDecision; audio = $transcode.audioDecision; subtitles = $transcode.subtitleDecision
            from = $transcode.sourceVideoCodec; to = $transcode.videoCodec; audio_from = $transcode.sourceAudioCodec; audio_to = $transcode.audioCodec
            container = $transcode.container; protocol = $transcode.protocol; speed = $transcode.speed; progress = $transcode.progress
            throttled = [bool]$transcode.throttled; hw_decode = $transcode.transcodeHwDecoding; hw_encode = $transcode.transcodeHwEncoding } })
    }
}

function Get-PlexSession($s) {
    $kind = $s.type; $player = $s.Player; $session = $s.Session; $transcode = $s.TranscodeSession
    $media = if ($s.Media) { @($s.Media)[0] } else { $null }
    if ($kind -eq 'episode') {
        $number = if ($null -ne $s.parentIndex -and $null -ne $s.index) { "S$($s.parentIndex)$($Dot)E$($s.index)" } else { '' }
        $title = $s.grandparentTitle; $sub = (@($number, $s.title) | Where-Object { $_ }) -join $Dot
        $thumb = if ($s.grandparentThumb) { $s.grandparentThumb } else { $s.thumb }
    } elseif ($kind -eq 'track') {
        $title = $s.title; $sub = (@($s.grandparentTitle, $s.parentTitle) | Where-Object { $_ }) -join $Dash
        $thumb = if ($s.parentThumb) { $s.parentThumb } else { $s.thumb }
    } else {
        $title = $s.title; $sub = if ($s.year) { "$($s.year)" } else { '' }; $thumb = $s.thumb
    }
    $decision = if (-not $transcode) { 'direct play' }
        elseif ($transcode.videoDecision -eq 'transcode' -or ($kind -eq 'track' -and $transcode.audioDecision -eq 'transcode')) { 'transcode' }
        else { 'direct stream' }       # the video is copied; only the audio or the container changes
    $resolution = "$($media.videoResolution)"
    $quality = if ($resolution -eq '4k') { '4K' } elseif ($resolution -eq 'sd') { 'SD' } elseif ($resolution -match '^\d+$') { "${resolution}p" }
        elseif ($resolution) { $resolution } else { "$($media.audioCodec)".ToUpper() }
    [ordered]@{
        id = "$(if ($session.id) { $session.id } else { $s.sessionKey })"; kind = $kind; title = $title; sub = $sub
        user = $s.User.title; player = $(if ($player.title) { $player.title } else { $player.product }); state = $player.state
        position_ms = $s.viewOffset; duration_ms = $s.duration; decision = $decision
        hw = [bool]($transcode -and ($transcode.transcodeHwEncoding -or $transcode.transcodeHwFullPipeline))
        quality = $quality; kbps = $session.bandwidth
        local = $(if ($session.location) { $session.location -ne 'wan' } else { $player.local -ne $false }); thumb = $thumb
        details = Get-PlexDetails $s $player $media $transcode
    }
}

function Get-PlexCount($key, [int]$type = 0) {
    $query = $(if ($type) { "type=$type&" } else { '' }) + 'X-Plex-Container-Start=0&X-Plex-Container-Size=0'
    (Invoke-Plex "/library/sections/$key/all?$query").totalSize
}

# Every 5 minutes: the library counts and the update check.
function Update-PlexLibraries {
    $script:plexLibraries = @(foreach ($d in (Invoke-Plex '/library/sections').Directory) {
        $library = [ordered]@{ title = $d.title; kind = $d.type; count = (Get-PlexCount $d.key) }
        if ($d.type -eq 'show') { $library.episodes = Get-PlexCount $d.key 4 }
        elseif ($d.type -eq 'artist') { $library.albums = Get-PlexCount $d.key 9; $library.tracks = Get-PlexCount $d.key 10 }
        $library
    })
    $updater = Invoke-Plex '/updater/status' -Optional
    $script:plexUpdate = $null
    if ($updater) { foreach ($r in $updater.Release) { if ($r.version) { $script:plexUpdate = $r.version; break } } }
    $script:plexCounted = Get-Date
}

# The server's own load and bandwidth history, for the graphs (needs Plex Pass).
function Get-PlexHistory {
    $resources = Invoke-Plex "/statistics/resources?timespan=$PlexStep" -Optional
    $rows = @()
    if ($resources -and $resources.StatisticsResources) { $rows = @($resources.StatisticsResources | Sort-Object { $_.at } | Select-Object -Last 60) }
    $load = $null
    if ($rows.Count) {
        $load = [ordered]@{
            cpu = @($rows | ForEach-Object { [Math]::Round([double]$_.hostCpuUtilization, 1) })
            mem = @($rows | ForEach-Object { [Math]::Round([double]$_.hostMemoryUtilization, 1) })
            plex_cpu = $rows[-1].processCpuUtilization; plex_mem = $rows[-1].processMemoryUtilization
        }
    }
    $bandwidth = Invoke-Plex "/statistics/bandwidth?timespan=$PlexStep" -Optional
    if ($null -eq $bandwidth) { return @{ load = $load; net = $null } }
    # One row per device and sample, at uneven times: add them up per 6 s, LAN and WAN apart.
    $buckets = @{}
    foreach ($r in $bandwidth.StatisticsBandwidth) {
        $k = [long][Math]::Floor([double]$r.at / $PlexStep)
        if (-not $buckets.ContainsKey($k)) { $buckets[$k] = @(0, 0) }
        $buckets[$k][$(if ($r.lan) { 0 } else { 1 })] += [long]$r.bytes
    }
    $end = [long][Math]::Floor([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() / $PlexStep)
    $start = $end
    foreach ($k in $buckets.Keys) { if ($k -gt $end) { $end = $k } }
    foreach ($k in $buckets.Keys) { if ($k -lt $start) { $start = $k } }
    $start = [Math]::Max($start, $end - 59)
    $lan = @(); $wan = @()
    for ($k = $start; $k -le $end; $k++) {
        $b = $buckets[[long]$k]
        $lan += $(if ($b) { [long][Math]::Floor($b[0] / $PlexStep) } else { 0 })
        $wan += $(if ($b) { [long][Math]::Floor($b[1] / $PlexStep) } else { 0 })
    }
    @{ load = $load; net = [ordered]@{ lan = $lan; wan = $wan } }
}

function Read-Plex {
    Read-PlexConfig
    if (-not $script:plex.server) { return @{ state = 'none' } }
    if (((Get-Date) - $script:plexCounted).TotalMinutes -ge 5) { Update-PlexLibraries }
    $root = Invoke-Plex '/'
    $thumbs = @{}
    $sessions = @(foreach ($s in (Invoke-Plex '/status/sessions').Metadata) {
        $session = Get-PlexSession $s
        if ($session.thumb) {
            $session.art = (Get-Hash $session.thumb).Substring(0, 16)
            $thumbs[$session.art] = @{ path = $session.thumb; square = ($session.kind -eq 'track') }
        }
        $session.Remove('thumb')
        $session
    })
    $script:plexThumbs = $thumbs
    $history = Get-PlexHistory
    $activities = @(foreach ($a in (Invoke-Plex '/activities').Activity) {
        if ($a.title) { [ordered]@{ title = $a.title; sub = $a.subtitle; progress = $a.progress } }
    })
    $version = "$($root.version)"
    [ordered]@{
        state = 'ok'; sessions = $sessions; load = $history.load; net = $history.net
        libraries = $script:plexLibraries; activities = $activities
        server = [ordered]@{
            name = $root.friendlyName; version = $version
            update = $(if ($script:plexUpdate -and $script:plexUpdate -ne $version) { $script:plexUpdate } else { $null })
            transcodes = $(if ($root.transcoderActiveVideoSessions) { $root.transcoderActiveVideoSessions } else { 0 })
        }
    }
}

# The posters the Car Thing asked for, small (it shows them 44 px wide).
function Get-PlexPosters {
    $posters = @{}
    foreach ($key in $script:plexNeed) {
        $thumb = $script:plexThumbs[$key]
        if (-not $thumb) { continue }
        $width = if ($thumb.square) { 144 } else { 96 }
        $path = "/photo/:/transcode?width=$width&height=144&minSize=1&upscale=1&url=" + [Uri]::EscapeDataString($thumb.path)
        try { $bytes = Invoke-Plex $path -Raw } catch { Write-Log "plex poster: $($_.Exception.Message)"; continue }
        if ($bytes -and $bytes.Length -le 3MB) { $posters[$key] = [Convert]::ToBase64String($bytes) }
    }
    $posters
}

# Every 3 s while Plex is on screen, else every 30 s (called more often; it waits on its own).
# When the server doesn't answer, it tries again in 30 s, so the other pages don't wait.
# Sends only changes, and at least every 10 s.
function Update-Plex {
    if ((Get-Date) -lt $script:plexNext) { return }
    $script:plexNext = (Get-Date).AddSeconds($(if (Test-Visible 'plex') { 3 } else { 30 }))
    try {
        $state = Read-Plex
        if ($script:plexNeed.Count) { $state.posters = Get-PlexPosters }
    } catch {
        $script:plexNext = (Get-Date).AddSeconds(30)
        # A .NET call's error comes wrapped ("Exception calling GetResponse ..."): show the cause.
        $message = if ($_.Exception.InnerException) { $_.Exception.InnerException.Message } else { $_.Exception.Message }
        if ($message -notmatch 'Plex token') { $message = "$($script:plex.server): $message" }
        $state = @{ state = 'error'; error = $message }
    }
    if ($state.error -ne $script:plexError) {
        $script:plexError = $state.error
        if ($state.error) { Write-Log "plex: $($state.error)" }
    }
    $key = $state | ConvertTo-Json -Compress -Depth 6
    if ($key -eq $script:plexSent -and ((Get-Date) - $script:plexSentAt).TotalSeconds -lt 10) { return }
    $reply = Send-Json 'plex' $state 8       # a stream's details nest deeper
    $script:plexSent = $key
    $script:plexSentAt = Get-Date
    $script:plexNeed = @($reply.need_art | Where-Object { $_ })
}

# --- TrueNAS. A TrueNAS server on the local network: pools, disk temperatures, alerts,
# apps and running jobs, through its JSON-RPC API over a WebSocket (wss://server/api/current). truenas.txt
# next to the log holds the server's address on the first line and an API key on the second.
# Always over https: TrueNAS revokes a key that is ever sent over plain http.
$TrueNasFile = Join-Path (Split-Path $LogFile) 'truenas.txt'
Add-Type @'
using System;
using System.Net.WebSockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
public static class ThingdeckNas {
    // Log in with the key, run the calls in order and return their results as one JSON array.
    // calls: JSON-RPC method names; args: each call's params as a JSON array.
    public static string Call(string host, string key, string[] calls, string[] args) {
        using (var ws = new ClientWebSocket()) {
            var cancel = new CancellationTokenSource(5000).Token;      // the main loop waits on this: keep it short
            ws.ConnectAsync(new Uri("wss://" + host + "/api/current"), cancel).Wait();
            var results = new StringBuilder("[");
            for (int i = -1; i < calls.Length; i++) {
                string method = i < 0 ? "auth.login_with_api_key" : calls[i];
                string param = i < 0 ? "[" + Quote(key) + "]" : args[i];
                string request = "{\"jsonrpc\":\"2.0\",\"id\":" + (i + 2) + ",\"method\":\"" + method + "\",\"params\":" + param + "}";
                ws.SendAsync(new ArraySegment<byte>(Encoding.UTF8.GetBytes(request)), WebSocketMessageType.Text, true, cancel).Wait();
                string reply;       // skip notifications and other replies: wait for this call's id
                var mine = new Regex("\"id\"\\s*:\\s*" + (i + 2) + "\\s*[,}]");
                do { reply = Receive(ws, cancel); } while (!mine.IsMatch(reply));
                if (i < 0) {
                    if (!Regex.IsMatch(reply, "\"result\"\\s*:\\s*true"))
                        throw new Exception("TrueNAS refused the API key: check the second line of truenas.txt");
                    continue;
                }
                if (i > 0) results.Append(',');
                results.Append(reply);
            }
            try { ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", cancel).Wait(2000); } catch { }
            return results.Append(']').ToString();
        }
    }
    static string Receive(ClientWebSocket ws, CancellationToken cancel) {
        var buffer = new byte[65536];
        var message = new System.IO.MemoryStream();
        WebSocketReceiveResult got;
        do {
            got = ws.ReceiveAsync(new ArraySegment<byte>(buffer), cancel).Result;
            if (got.MessageType == WebSocketMessageType.Close) throw new Exception("TrueNAS closed the connection");
            message.Write(buffer, 0, got.Count);
        } while (!got.EndOfMessage);
        return Encoding.UTF8.GetString(message.ToArray());
    }
    static string Quote(string s) {
        return "\"" + s.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
    }
}
'@
$nas = @{ host = $null; key = $null }
$nasNext = [DateTime]::MinValue
$nasSent = $null; $nasSentAt = [DateTime]::MinValue; $nasError = $null

function Read-TrueNasConfig {
    $lines = @()
    if (Test-Path $TrueNasFile) { $lines = @(Get-Content $TrueNasFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') }) }
    $address = $(if ($lines.Count) { $lines[0] -replace '^[a-z]+://', '' -replace '/.*$', '' } else { $null })
    [ThingdeckHueTls]::Nas = $(if ($address) { ($address -split ':')[0] } else { '' })
    $script:nas = @{ host = $address; key = $(if ($lines.Count -gt 1) { $lines[1] } else { $null }) }
}

# The scheduled tasks, so the Car Thing can show the next one: query, kind, fields that name it.
$TrueNasTasks = @(
    @{ call = 'pool.scrub.query'; kind = 'scrub'; names = @('pool_name') }
    @{ call = 'pool.snapshottask.query'; kind = 'snapshot'; names = @('dataset') }
    @{ call = 'replication.query'; kind = 'replication'; names = @('name') }
    @{ call = 'cloudsync.query'; kind = 'cloudsync'; names = @('description') }
    @{ call = 'rsynctask.query'; kind = 'rsync'; names = @('desc', 'path') }
    @{ call = 'cronjob.query'; kind = 'cron'; names = @('description', 'command') }
)

$nasFailed = @{}      # TrueNAS calls that failed: logged once, until they work again (an older TrueNAS lacks some)
function Read-TrueNas {
    Read-TrueNasConfig
    if (-not $script:nas.host) { return @{ state = 'none' } }
    if (-not $script:nas.key) { throw 'No API key: put one on the second line of truenas.txt' }
    $calls = @('system.info', 'pool.query', 'disk.temperatures', 'alert.list', 'app.query', 'core.get_jobs', 'update.status') + @($TrueNasTasks | ForEach-Object { $_.call })
    $params = '[]',
        '[[], {"select": ["name", "status", "healthy", "size", "allocated", "free", "scan"]}]',
        '[[]]',
        '[]',
        '[[], {"select": ["name", "state", "upgrade_available"]}]',
        '[[["state", "=", "RUNNING"]]]',                   # no "select": TrueNAS fails on it for jobs
        '[]'                                               # update.status: a newer TrueNAS to install (25.10 and later)
    $params = @($params) + @($TrueNasTasks | ForEach-Object { '[]' })
    $replies = [ThingdeckNas]::Call($script:nas.host, $script:nas.key, $calls, $params) | ConvertFrom-Json
    $result = @{}
    for ($i = 0; $i -lt $calls.Count; $i++) {
        $reply = $replies[$i]
        if ($reply.error) {
            $reason = $(if ($reply.error.data.reason) { $reply.error.data.reason } else { $reply.error.message })
            if ($calls[$i] -eq 'system.info') { throw "TrueNAS: $reason" }
            if (-not $script:nasFailed.ContainsKey($calls[$i])) { $script:nasFailed[$calls[$i]] = $true; Write-Log "truenas $($calls[$i]): $reason" }
            $result[$calls[$i]] = $null
        } else { $script:nasFailed.Remove($calls[$i]); $result[$calls[$i]] = $reply.result }
    }
    $info = $result['system.info']
    $disks = @()
    if ($result['disk.temperatures']) {
        foreach ($p in $result['disk.temperatures'].PSObject.Properties) {
            $t = $(if ($p.Value -is [ValueType]) { $p.Value } elseif ($p.Value) { $p.Value.temp } else { $null })
            $disks += [ordered]@{ name = $p.Name; temp = $t }
        }
    }
    @{
        state = 'ok'
        system = [ordered]@{ host = $info.hostname; version = $info.version; uptime = [long]$info.uptime_seconds
            memory = $info.physmem; cores = $info.cores; load = @($info.loadavg)
            update = $(if ($result['update.status'].status.new_version) { $result['update.status'].status.new_version.version } else { $null }) }
        pools = @(foreach ($p in $result['pool.query']) {
            [ordered]@{ name = $p.name; status = $p.status; healthy = [bool]$p.healthy
                size = $p.size; used = $p.allocated; free = $p.free
                scan = $(if ($p.scan -and $p.scan.state -eq 'SCANNING') { [ordered]@{ kind = $p.scan.function; percent = $p.scan.percentage } } else { $null })
                # The last scrub's end, in ms
                scrubbed = $(if ($p.scan -and $p.scan.function -eq 'SCRUB' -and $p.scan.state -eq 'FINISHED' -and $p.scan.end_time) { $p.scan.end_time.'$date' } else { $null }) }
        })
        disks = @($disks | Sort-Object { $_.name })
        alerts = @(foreach ($a in $result['alert.list']) {
            if (-not $a.dismissed -and $a.level -ne 'INFO') { [ordered]@{ level = $a.level; text = ($a.formatted -replace '<[^>]+>', '').Trim() } }
        })
        apps = @(foreach ($a in $result['app.query']) { [ordered]@{ name = $a.name; state = $a.state; update = [bool]$a.upgrade_available } })
        # A running job: its method (the Car Thing names it), the pool or app it works on, and its progress.
        jobs = @(foreach ($j in $result['core.get_jobs']) {
            $first = @($j.arguments)[0]
            [ordered]@{ method = $j.method; target = $(if ($first -is [string] -and $first.Length -le 64) { $first } else { $null })
                percent = $j.progress.percent; text = $(if ($j.progress.description) { $j.progress.description } else { $null })
                started = $j.time_started.'$date' }
        })
        # The enabled tasks with a schedule; the Car Thing works out when each runs next
        # (a replication without "auto" runs only by hand).
        tasks = @(foreach ($t in $TrueNasTasks) {
            foreach ($x in $result[$t.call]) {
                if ($x.enabled -and $x.auto -ne $false -and $x.schedule) {
                    $name = "$(@($t.names | ForEach-Object { $x.$_ } | Where-Object { $_ })[0])"
                    [ordered]@{ kind = $t.kind; name = $name.Substring(0, [Math]::Min(60, $name.Length)); schedule = $x.schedule; threshold = $x.threshold }
                }
            }
        })
    }
}

# Every 10 s while the NAS page is on screen, else every 60 s; 60 s after an error.
# Sends only changes, and at least every 30 s.
function Update-TrueNas {
    if ((Get-Date) -lt $script:nasNext) { return }
    $script:nasNext = (Get-Date).AddSeconds($(if (Test-Visible 'truenas') { 10 } else { 60 }))
    try { $state = Read-TrueNas }
    catch {
        $script:nasNext = (Get-Date).AddSeconds(60)
        $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
        $message = $e.Message
        if ($message -notmatch 'truenas.txt|^TrueNAS') { $message = "$($script:nas.host): $message" }
        $state = @{ state = 'error'; error = $message }
    }
    if ($state.error -ne $script:nasError) {
        $script:nasError = $state.error
        if ($state.error) { Write-Log "truenas: $($state.error)" }
    }
    $key = $state | ConvertTo-Json -Compress -Depth 5
    if ($key -eq $script:nasSent -and ((Get-Date) - $script:nasSentAt).TotalSeconds -lt 30) { return }
    Send-Json 'truenas' $state 5 | Out-Null
    $script:nasSent = $key
    $script:nasSentAt = Get-Date
}

# --- Weather, for the large clocks. weather.txt next to the log: a place on the first line
# ("Paris", "Paris, France", or "48.85, 2.35"), and "fahrenheit" on a second line for °F.
# Open-Meteo (open-meteo.com) needs no key; a name is looked up once with its geocoder.
$WeatherFile = Join-Path (Split-Path $LogFile) 'weather.txt'
$weatherPlace = @{ query = $null }      # the looked-up place: query, lat, lon, name
$weatherNext = [DateTime]::MinValue
$weatherSent = $null; $weatherSentAt = [DateTime]::MinValue; $weatherError = $null

function Read-Weather {
    $lines = @()
    if (Test-Path $WeatherFile) { $lines = @(Get-Content $WeatherFile -Encoding UTF8 | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') }) }
    if (-not $lines.Count) { return @{ state = 'none' } }
    $query = $lines[0]
    $fahrenheit = $lines.Count -gt 1 -and $lines[1] -match '^f'
    if ($script:weatherPlace.query -ne $query) {
        if ($query -match '^\s*(-?\d+(\.\d+)?)\s*,\s*(-?\d+(\.\d+)?)\s*$') {
            $script:weatherPlace = @{ query = $query; lat = [double]$Matches[1]; lon = [double]$Matches[3]; name = $null }
        } else {
            # "Paris, France": the geocoder takes the name; a country after the comma picks among the matches.
            $name, $country = ($query -split ',', 2) | ForEach-Object { $_.Trim() }
            $found = Invoke-RestMethod -TimeoutSec 5 -Uri ("https://geocoding-api.open-meteo.com/v1/search?count=10&language=en&name=" + [uri]::EscapeDataString($name))
            $hit = @($found.results | Where-Object { -not $country -or $_.country -like "$country*" -or $_.country_code -eq $country })[0]
            if (-not $hit) { throw "Open-Meteo doesn't know `"$query`": check the first line of weather.txt" }
            $script:weatherPlace = @{ query = $query; lat = $hit.latitude; lon = $hit.longitude; name = $hit.name }
        }
    }
    $p = $script:weatherPlace
    $url = 'https://api.open-meteo.com/v1/forecast?latitude={0}&longitude={1}&current=temperature_2m,weather_code,is_day' -f
        $p.lat.ToString([Globalization.CultureInfo]::InvariantCulture), $p.lon.ToString([Globalization.CultureInfo]::InvariantCulture)
    if ($fahrenheit) { $url += '&temperature_unit=fahrenheit' }
    $w = Invoke-RestMethod -TimeoutSec 5 -Uri $url
    @{ state = 'ok'; place = $p.name; temp = $w.current.temperature_2m; code = $w.current.weather_code; day = [bool]$w.current.is_day
        unit = $(if ($fahrenheit) { 'F' } else { 'C' }) }
}

# Every 15 minutes (5 after an error); sends changes, and at least every 30 minutes.
function Update-Weather {
    if ((Get-Date) -lt $script:weatherNext) { return }
    $script:weatherNext = (Get-Date).AddMinutes(15)
    try { $state = Read-Weather }
    catch {
        $script:weatherNext = (Get-Date).AddMinutes(5)
        $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
        $state = @{ state = 'error'; error = $e.Message }
    }
    if ($state.error -ne $script:weatherError) {
        $script:weatherError = $state.error
        if ($state.error) { Write-Log "weather: $($state.error)" }
    }
    $key = $state | ConvertTo-Json -Compress
    if ($key -eq $script:weatherSent -and ((Get-Date) - $script:weatherSentAt).TotalMinutes -lt 30) { return }
    [void](Send-Json 'weather' $state)
    $script:weatherSent = $key
    $script:weatherSentAt = Get-Date
}

# Links the Car Thing asks to open: https links of known meeting services, and GitHub.
$OpenableUrl ='^https://(meet\.google\.com|([a-z0-9-]+\.)?zoom\.us|teams\.microsoft\.com|teams\.live\.com|github\.com)/'

if ($Once) {
    $np = Get-NowPlaying
    if ($np) { $np.Remove('thumbnail'); $np | ConvertTo-Json } else { 'nothing playing' }
    Start-Sleep -Seconds 1
    Get-Stats | ConvertTo-Json
    Get-Focus | ConvertTo-Json
    Read-Audio | ConvertTo-Json
    $audioLists | ConvertTo-Json -Depth 4
    try { Read-Plex | ConvertTo-Json -Depth 6 } catch { "plex: $($_.Exception.Message)" }
    try { Read-Weather | ConvertTo-Json } catch { "weather: $($_.Exception.Message)" }
    try { Read-TrueNas | ConvertTo-Json -Depth 5 } catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }; "truenas: $($e.Message)" }
    exit
}

$adbExe = Find-Adb
# This script's fingerprint: deploy.sh puts the repo's on the Car Thing, which says when they differ.
$AgentVersion = (Get-FileHash $PSCommandPath -Algorithm SHA1).Hash.Substring(0, 12).ToLower()

# Run adb as a plain process and go by its exit code. Not "& adb ... 2>$null": after a
# reboot adb first prints "daemon not running; starting now" on stderr, which Windows
# PowerShell turns into an error, so the link never came up. Its output isn't captured
# either: the adb server it may start inherits those handles and keeps them open.
function Invoke-Adb([string]$arguments) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo $script:adbExe, $arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($psi)
    if (-not $process.WaitForExit(20000)) { try { $process.Kill() } catch { }; return $false }
    $process.ExitCode -eq 0
}
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
$calendarSoon = $false       # the Car Thing says an event starts within 15 minutes
$calendars = $null
$fetchedHash = $null

while ($true) {
    try {
        if (-not $tunnelUp) {
            # Only now call adb: on start and after a failed send (unplugged, rebooted).
            if (-not (Invoke-Adb 'start-server') -or -not (Invoke-Adb "forward tcp:$Port tcp:8765")) { Start-Sleep -Seconds 3; continue }
            $tunnelUp = $true
            # A new connection (start, replug, the Car Thing restarted): it may have lost
            # everything, so send it all again now instead of at each part's next turn.
            $lastSignature = $null; $coverSentFor = ''; $lastHost = $null; $calendarHash = $null
            $gitHubFetched = [DateTime]::MinValue
            $audioSent = $null; $audioListsSent = $null
            $hueSent = $null; $hueFetched = [DateTime]::MinValue
            $plexSent = $null; $plexNext = [DateTime]::MinValue
            $nasSent = $null; $nasNext = [DateTime]::MinValue
            $weatherSent = $null; $weatherNext = [DateTime]::MinValue
            $mixerSent = $null; $mixerSentAt = [DateTime]::MinValue
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
        # GitHub every minute, or every 15 s while a workflow runs and Code is on screen.
        if (((Get-Date) - $gitHubFetched).TotalSeconds -ge $(if ($gitHubBusy -and (Test-Visible 'github')) { 15 } else { 60 })) {
            $gitHubFetched = Get-Date
            $gitHub = Get-GitHub
            $gitHubBusy = [bool]($gitHub.runs | Where-Object { $_.status -ne 'completed' })
            [void](Send-Json 'github' $gitHub)
        }
        # The calendars every 5 minutes; every minute while the Calendar page is on screen or an
        # event starts within 15 minutes, so a late change still reaches its reminder.
        if (((Get-Date) - $calendarFetched).TotalMinutes -ge $(if ($calendarSoon -or (Test-Visible 'agenda')) { 1 } else { 5 })) {
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
                agent = @{ os = 'windows'; version = $AgentVersion }      # the Car Thing compares it with the repo's
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
            try { Update-Audio; Update-Mixer; Update-Hue; Update-Plex; Update-TrueNas; Update-Weather } catch { Write-Log "update failed: $($_.Exception.Message)"; $tunnelUp = $false }
        }
        Start-Sleep -Milliseconds 250
    }
}
