<#
Install the now-playing agent so it starts at logon (no admin needed).

  powershell -ExecutionPolicy Bypass -File install.ps1              # install + start
  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall   # stop + remove

Copies nowplaying.ps1 to %USERPROFILE%\.streamthing, builds a small windowless
launcher next to it, and registers a Task Scheduler task for the current user that
starts it at logon. The log is %USERPROFILE%\.streamthing\nowplaying.log.
#>
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$taskName = 'streamthing now playing'
# Not %LOCALAPPDATA%: when run from a packaged (MSIX) app, writes there are virtualized
# into the package and Task Scheduler can't see them. %USERPROFILE% is never redirected.
$dir = Join-Path $env:USERPROFILE '.streamthing'
$script = Join-Path $dir 'nowplaying.ps1'
$launcher = Join-Path $dir 'streamthing-nowplaying.exe'

# A console program started by Task Scheduler gets a visible console window, and
# "conhost --headless" exits at once there. A GUI-subsystem launcher has no window.
$launcherSource = @'
using System;
using System.Diagnostics;
using System.IO;
public static class StreamthingNowPlaying {
    public static int Main() {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                                         @"WindowsPowerShell\v1.0\powershell.exe");
        var psi = new ProcessStartInfo(powershell,
            "-NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(dir, "nowplaying.ps1") + "\"");
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        using (var p = Process.Start(psi)) { p.WaitForExit(); return p.ExitCode; }
    }
}
'@

function Stop-Agent {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Get-Process streamthing-nowplaying -ErrorAction SilentlyContinue | Stop-Process -Force
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -like '*streamthing*nowplaying.ps1*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Stop-Agent
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $script, $launcher -ErrorAction SilentlyContinue
    'Removed. (The log stays in ' + $dir + '.)'
    exit
}

if (-not (Get-Command adb.exe -ErrorAction SilentlyContinue) -and
    -not (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter adb.exe -ErrorAction SilentlyContinue)) {
    throw 'adb not found. Install it first: winget install Google.PlatformTools'
}

Stop-Agent
New-Item -ItemType Directory -Force $dir | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'nowplaying.ps1') $script -Force
Remove-Item $launcher -ErrorAction SilentlyContinue
Add-Type -TypeDefinition $launcherSource -OutputAssembly $launcher -OutputType WindowsApplication

$action = New-ScheduledTaskAction -Execute $launcher
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'Sends what is playing to the streamthing Car Thing over adb.' | Out-Null
Start-ScheduledTask -TaskName $taskName
"Installed and started. It runs at every logon; log: $dir\nowplaying.log"
