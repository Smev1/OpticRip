# Activate a specific window by title, verify it is foreground, then send keys.
param([string]$Title = "MakeMKV BETA", [string]$Keys = "", [int]$SleepMs = 700)
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class K2 {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint c);
}
"@
$p = Get-Process makemkv -ErrorAction Stop | Select-Object -First 1
$wins = New-Object System.Collections.ArrayList
[K2]::EnumWindows({ param($h,$l)
  $pp=0; [void][K2]::GetWindowThreadProcessId($h,[ref]$pp)
  if ($pp -eq $p.Id -and [K2]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 512; [void][K2]::GetWindowTextW($h,$sb,512)
    [void]$script:wins.Add([pscustomobject]@{h=$h;t=$sb.ToString()})
  }
  return $true }, [IntPtr]::Zero) | Out-Null
$t = $wins | Where-Object { $_.t -eq $Title } | Select-Object -First 1
if (-not $t) { "ERROR no window with exact title '$Title'; have: " + (($wins | ForEach-Object { "'" + $_.t + "'" }) -join ', '); exit 1 }
[void][K2]::ShowWindow($t.h, 9)   # SW_RESTORE
[void][K2]::BringWindowToTop($t.h)
[void][K2]::SetForegroundWindow($t.h)
Start-Sleep -Milliseconds 350
$fg = [K2]::GetForegroundWindow()
if ($fg -ne $t.h) {
  # last resort: attach input via AppActivate on title
  $sh = New-Object -ComObject WScript.Shell
  [void]$sh.AppActivate($p.Id); Start-Sleep -Milliseconds 300
  $fg = [K2]::GetForegroundWindow()
}
if ($fg -ne $t.h) { "REFUSING to send keys: foreground is not target (fg=$fg target=$($t.h))"; exit 2 }
if ($Keys) { [System.Windows.Forms.SendKeys]::SendWait($Keys); "sent '$Keys' to '$Title'" }
Start-Sleep -Milliseconds $SleepMs
