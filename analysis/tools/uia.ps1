# UIA driver for black-box observation of the MakeMKV GUI.
# Usage: uia.ps1 tree | click <name-regex> | expand <name-regex> | value <name-regex> | shot <file>
param([string]$cmd = "tree", [string]$arg = "", [int]$pid2 = 0)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing, System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class MC {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, int dx, int dy, uint d, IntPtr e);
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr c);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
"@
try { [void][MC]::SetProcessDpiAwarenessContext([IntPtr]-4) } catch {}

$A = [System.Windows.Automation.AutomationElement]
$proc = if ($pid2) { Get-Process -Id $pid2 } else { Get-Process makemkv -ErrorAction Stop | Select-Object -First 1 }
$cond = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, $proc.Id)
$win = $A::RootElement.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
if (-not $win) { "no window"; exit 1 }

function Find-All([string]$re) {
  $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  $out = @()
  foreach ($e in $all) { if ($e.Current.Name -match $re) { $out += $e } }
  return $out
}
function Pattern($e, $p) { $o = $null; if ($e.TryGetCurrentPattern($p, [ref]$o)) { return $o } return $null }

switch ($cmd) {
  "tree" {
    $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($e in $all) {
      $c = $e.Current
      $val = ""
      $vp = Pattern $e ([System.Windows.Automation.ValuePattern]::Pattern)
      if ($vp) { try { $val = " value='$($vp.Current.Value)'" } catch {} }
      $sel = Pattern $e ([System.Windows.Automation.SelectionItemPattern]::Pattern)
      $s2 = ""; if ($sel) { $s2 = " selected=$($sel.Current.IsSelected)" }
      "type=$($c.ControlType.ProgrammaticName) name='$($c.Name)' autoid='$($c.AutomationId)' class='$($c.ClassName)' en=$($c.IsEnabled) off=$($c.IsOffscreen)$s2$val"
    }
  }
  "click" {
    $hits = Find-All $arg
    if (-not $hits) { "NO MATCH $arg"; exit 2 }
    foreach ($h in $hits) {
      $ip = Pattern $h ([System.Windows.Automation.InvokePattern]::Pattern)
      if ($ip) { $ip.Invoke(); "invoked '$($h.Current.Name)'"; break }
      $tp = Pattern $h ([System.Windows.Automation.TogglePattern]::Pattern)
      if ($tp) { $tp.Toggle(); "toggled '$($h.Current.Name)'"; break }
      $sp = Pattern $h ([System.Windows.Automation.SelectionItemPattern]::Pattern)
      if ($sp) { $sp.Select(); "selected '$($h.Current.Name)'"; break }
      "no invokable match"
    }
    Start-Sleep -Milliseconds 700
  }
  "expand" {
    $hits = Find-All $arg
    foreach ($h in $hits) {
      $ep = Pattern $h ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
      if ($ep) { $ep.Expand(); "expanded '$($h.Current.Name)'"; Start-Sleep -Milliseconds 500; break }
    }
  }
  "value" {
    $hits = Find-All $arg
    foreach ($h in $hits) { $vp = Pattern $h ([System.Windows.Automation.ValuePattern]::Pattern); if ($vp) { "value='$($vp.Current.Value)'" } }
  }
  "windows" {
    $all = $A::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($e in $all) { if ($e.Current.ProcessId -eq $proc.Id) { "win name='$($e.Current.Name)' class='$($e.Current.ClassName)'" } }
  }
  "all" {
    $all = $A::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($w in $all) {
      if ($w.Current.ProcessId -ne $proc.Id) { continue }
      "WINDOW name='$($w.Current.Name)' class='$($w.Current.ClassName)' type=$($w.Current.ControlType.ProgrammaticName)"
      $kids = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
      foreach ($e in $kids) {
        $c = $e.Current
        $val = ""
        $vp = Pattern $e ([System.Windows.Automation.ValuePattern]::Pattern)
        if ($vp) { try { $val = " value='$($vp.Current.Value)'" } catch {} }
        $sel = Pattern $e ([System.Windows.Automation.SelectionItemPattern]::Pattern)
        $s2 = ""; if ($sel) { $s2 = " selected=$($sel.Current.IsSelected)" }
        $ck = Pattern $e ([System.Windows.Automation.TogglePattern]::Pattern)
        $s3 = ""; if ($ck) { $s3 = " check=$($ck.Current.ToggleState)" }
        "  type=$($c.ControlType.ProgrammaticName) name='$($c.Name)' autoid='$($c.AutomationId)' class='$($c.ClassName)' en=$($c.IsEnabled) off=$($c.IsOffscreen)$s2$s3$val"
      }
    }
  }
  "menudump" {
    $hits = Find-All $arg
    foreach ($h in $hits) {
      $ep = Pattern $h ([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
      if ($ep) { $ep.Expand(); break }
    }
    Start-Sleep -Milliseconds 600
    $tops = $A::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($w in $tops) {
      if ($w.Current.ProcessId -ne $proc.Id) { continue }
      "WINDOW name='$($w.Current.Name)' class='$($w.Current.ClassName)' type=$($w.Current.ControlType.ProgrammaticName)"
      $kids = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
      foreach ($e in $kids) {
        $c = $e.Current
        if ($c.ControlType.ProgrammaticName -match 'MenuItem|Menu$|MenuItem') {
          "   MENU name='$($c.Name)' class='$($c.ClassName)' en=$($c.IsEnabled) off=$($c.IsOffscreen) autoid='$($c.AutomationId)'"
        }
      }
    }
  }
  "mouseclick" {
    $hits = Find-All $arg
    if (-not $hits) { "NO MATCH $arg"; exit 2 }
    $h = $hits[0]
    $r = $h.Current.BoundingRectangle
    $x = [int]($r.X + $r.Width/2); $y = [int]($r.Y + $r.Height/2)
    [void][MC]::SetCursorPos($x, $y); Start-Sleep -Milliseconds 150
    [MC]::mouse_event(0x0002,0,0,0,[IntPtr]::Zero); Start-Sleep -Milliseconds 60
    [MC]::mouse_event(0x0004,0,0,0,[IntPtr]::Zero)
    "mouseclicked '$($h.Current.Name)' at ($x,$y) rect=$([int]$r.X),$([int]$r.Y),$([int]$r.Width),$([int]$r.Height)"
    Start-Sleep -Milliseconds 900
  }
  "shot" {
    $r = $win.Current.BoundingRectangle
    $bmp = New-Object System.Drawing.Bitmap ([int]$r.Width), ([int]$r.Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen([int]$r.X, [int]$r.Y, 0, 0, $bmp.Size)
    $bmp.Save($arg, [System.Drawing.Imaging.ImageFormat]::Png)
    "saved $arg ($([int]$r.Width)x$([int]$r.Height))"
  }
}
