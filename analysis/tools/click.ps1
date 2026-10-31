# Click at window-relative coords (x,y) in a titled window; also supports key sending.
param(
  [string]$Title = "",
  [int]$X = -1, [int]$Y = -1,
  [string]$Keys = "",       # e.g. "{TAB}" or "^c"
  [int]$Double = 0
)
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class CW {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr c);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, uint d, IntPtr e);
  public struct RECT { public int L,T,R,B; }
}
"@

try { [void][CW]::SetProcessDpiAwarenessContext([IntPtr]-4) } catch { [void][CW]::SetProcessDPIAware() }
$p = Get-Process makemkv -ErrorAction Stop | Select-Object -First 1
$wins = New-Object System.Collections.ArrayList
[CW]::EnumWindows({ param($h,$l)
  $pp=0; [void][CW]::GetWindowThreadProcessId($h,[ref]$pp)
  if ($pp -eq $p.Id -and [CW]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 512; [void][CW]::GetWindowTextW($h,$sb,512)
    [void]$script:wins.Add([pscustomobject]@{h=$h;t=$sb.ToString()})
  }
  return $true }, [IntPtr]::Zero) | Out-Null
$target = $wins | Where-Object { $_.t -match $Title } | Select-Object -First 1
if (-not $target) { "no window matching '$Title'"; exit 1 }
[void][CW]::SetForegroundWindow($target.h); Start-Sleep -Milliseconds 250
if ($X -ge 0 -and $Y -ge 0) {
  $r = New-Object CW+RECT; [void][CW]::GetWindowRect($target.h,[ref]$r)
  [void][CW]::SetCursorPos($r.L + $X, $r.T + $Y); Start-Sleep -Milliseconds 120
  [CW]::mouse_event(0x0002,0,0,0,[IntPtr]::Zero); Start-Sleep -Milliseconds 60
  [CW]::mouse_event(0x0004,0,0,0,[IntPtr]::Zero)
  if ($Double) { Start-Sleep -Milliseconds 80; [CW]::mouse_event(0x0002,0,0,0,[IntPtr]::Zero); [CW]::mouse_event(0x0004,0,0,0,[IntPtr]::Zero) }
  "clicked '$($target.t)' at window+($X,$Y) screen=($($r.L+$X),$($r.T+$Y))"
}
if ($Keys) { Start-Sleep -Milliseconds 200; [System.Windows.Forms.SendKeys]::SendWait($Keys); "keys $Keys" }
