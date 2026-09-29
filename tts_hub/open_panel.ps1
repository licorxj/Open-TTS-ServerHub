<#
  Open the LcTTS panel window at a fixed size.

  Why this script exists:
  Chrome/Edge "app" windows persist their geometry in the browser profile, so once the
  panel window has ever been resized, "--window-size" is ignored on later launches and
  the window comes back at the remembered size. Closing the window is not enough.
  So we launch the browser and then force the window geometry with Win32 APIs.

  Usage:
    powershell -ExecutionPolicy Bypass -File tts_hub\open_panel.ps1 -Browser "<exe>" -Url "<url>" [-Mode app|window]
#>

param(
    [Parameter(Mandatory = $true)] [string]$Browser,
    [Parameter(Mandatory = $true)] [string]$Url,
    [string]$Mode = 'app',
    [int]$W = 1920,
    [int]$H = 1080,
    [int]$X = 0,
    [int]$Y = 0
)

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class LcWin {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr hAfter, int x, int y, int cx, int cy, uint f);
}
'@

$titlePattern = '*LcTTS*'

# 1) Close an already-open panel window so we start from a clean window.
Get-Process chrome, msedge -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -like $titlePattern } |
    ForEach-Object { $_.CloseMainWindow() | Out-Null }
Start-Sleep -Milliseconds 900

# 2) Launch the browser.
if ($Mode -eq 'app') {
    Start-Process $Browser -ArgumentList "--app=$Url"
}
else {
    Start-Process $Browser -ArgumentList '--new-window', $Url
}

# 3) Wait for the window, then force its geometry.
for ($i = 0; $i -lt 80; $i++) {
    Start-Sleep -Milliseconds 500
    $p = Get-Process chrome, msedge -ErrorAction SilentlyContinue |
         Where-Object { $_.MainWindowTitle -like $titlePattern } |
         Select-Object -First 1
    if ($p) {
        Start-Sleep -Milliseconds 700
        [LcWin]::ShowWindow($p.MainWindowHandle, 9) | Out-Null          # SW_RESTORE
        [LcWin]::SetWindowPos($p.MainWindowHandle, [IntPtr]::Zero, $X, $Y, $W, $H, 0x0040) | Out-Null
        break
    }
}
