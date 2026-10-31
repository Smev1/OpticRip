# Robust probe: enumerate process windows, focus one, dump UIA + MSAA tree, optional PrintWindow shot.
param(
  [string]$Title = "",       # regex on window title; default = focused/first visible
  [string]$Shot = "",        # output png path
  [switch]$Quiet
)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing, Accessibility
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices; using System.Drawing;
public class PW {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out object ppv);
  public struct RECT { public int L, T, R, B; }
  public static string Txt(IntPtr h) { var sb = new StringBuilder(512); GetWindowTextW(h, sb, 512); return sb.ToString(); }
  public static string Cls(IntPtr h) { var sb = new StringBuilder(256); GetClassNameW(h, sb, 256); return sb.ToString(); }
}
"@

$p = Get-Process makemkv -ErrorAction Stop | Select-Object -First 1
$wins = New-Object System.Collections.ArrayList
[PW]::EnumWindows({ param($h,$l)
  $pp = 0; [void][PW]::GetWindowThreadProcessId($h, [ref]$pp)
  if ($pp -eq $p.Id) { [void]$script:wins.Add([pscustomobject]@{h=$h; t=[PW]::Txt($h); c=[PW]::Cls($h); v=[PW]::IsWindowVisible($h)}) }
  return $true }, [IntPtr]::Zero) | Out-Null

if (-not $Quiet) { $wins | ForEach-Object { "HWND $($_.h) vis=$($_.v) class=$($_.c) title='$($_.t)'" } }

$target = $wins | Where-Object { $_.t -match $Title -and $_.v } | Select-Object -First 1
if (-not $target) { $target = $wins | Where-Object { $_.v -and $_.t } | Select-Object -First 1 }
if (-not $target) { "no visible window"; exit 1 }
"=== TARGET $($target.h) '$($target.t)' class=$($target.c)"
[void][PW]::SetForegroundWindow($target.h)
Start-Sleep -Milliseconds 400

$e = [System.Windows.Automation.AutomationElement]::FromHandle($target.h)
$kids = $e.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
"UIA descendants=$($kids.Count)"
foreach ($k in $kids) {
  $c = $k.Current
  $val = ""
  $o = $null
  if ($k.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$o)) { try { $val = " value='$($o.Current.Value)'" } catch {} }
  $s = ""
  if ($k.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$o)) { $s = " sel=$($o.Current.IsSelected)" }
  $chk = ""
  if ($k.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$o)) { $chk = " check=$($o.Current.ToggleState)" }
  "  $($c.ControlType.ProgrammaticName) '$($c.Name)' class=$($c.ClassName) id=$($c.AutomationId) en=$($c.IsEnabled)$s$chk$val"
}

$IID = [Guid]"618736E0-3C3D-11CF-810C-00AA00389B71"
$acc = $null
$hr = [PW]::AccessibleObjectFromWindow($target.h, [uint32]0xFFFFFFFC, [ref]$IID, [ref]$acc)
"MSAA hr=$hr"
if ($acc) {
  "MSAA children=$($acc.accChildCount) name='$($acc.accName(0))'"
  $n = [Math]::Min($acc.accChildCount, 120)
  for ($i = 1; $i -le $n; $i++) {
    try {
      $nm = $acc.accName($i); $rl = $acc.accRole($i); $st = $acc.accState($i); $vl = ""
      try { $vl = $acc.accValue($i) } catch {}
      "  [$i] '$nm' role=$rl state=$st val='$vl'"
    } catch { }
  }
}

if ($Shot) {
  $r = New-Object PW+RECT
  [void][PW]::GetWindowRect($target.h, [ref]$r)
  $w = [Math]::Max(1, $r.R - $r.L); $h2 = [Math]::Max(1, $r.B - $r.T)
  $bmp = New-Object System.Drawing.Bitmap $w, $h2
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $hdc = $g.GetHdc()
  $ok = [PW]::PrintWindow($target.h, $hdc, 2)
  $g.ReleaseHdc($hdc); $g.Dispose()
  $bmp.Save($Shot, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
  "SHOT $Shot ${w}x${h2} ok=$ok"
}
