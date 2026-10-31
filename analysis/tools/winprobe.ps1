# Black-box window/menu enumerator for the MakeMKV GUI study.
# Dumps top-level windows, child controls, native menu tree and status text.
param([switch]$Kill)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetMenu(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetMenuItemCount(IntPtr m);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetMenuStringW(IntPtr m, uint i, StringBuilder s, int n, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr GetSubMenu(IntPtr m, int i);
  [DllImport("user32.dll")] public static extern uint GetMenuState(IntPtr m, uint i, uint flags);
  [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageW(IntPtr h, uint msg, IntPtr w, StringBuilder l);
}
"@

function Txt([IntPtr]$h, [int]$n=512) { $sb = New-Object System.Text.StringBuilder $n; [void][W]::GetWindowTextW($h,$sb,$n); $sb.ToString() }
function Cls([IntPtr]$h) { $sb = New-Object System.Text.StringBuilder 256; [void][W]::GetClassNameW($h,$sb,256); $sb.ToString() }

function DumpMenu([IntPtr]$m, [int]$depth) {
  if ($m -eq [IntPtr]::Zero) { return }
  $n = [W]::GetMenuItemCount($m)
  for ($i=0; $i -lt $n; $i++) {
    $sb = New-Object System.Text.StringBuilder 512
    $len = [W]::GetMenuStringW($m, [uint32]$i, $sb, 512, 0x400) # MF_BYPOSITION
    $st = [W]::GetMenuState($m, [uint32]$i, 0x400)
    $label = $sb.ToString()
    $cmd = [W]::GetMenuItemID($m, $i)
    ("  " * $depth) + "[menu d=$depth i=$i] '$label' state=0x{0:x} id=$cmd" -f $st
    $sub = [W]::GetSubMenu($m, $i)
    if ($sub -ne [IntPtr]::Zero) { DumpMenu $sub ($depth+1) }
  }
}

$procs = Get-Process makemkv -ErrorAction SilentlyContinue
if ($Kill) { $procs | Stop-Process -Force; "killed"; exit }
if (-not $procs) { "NOT RUNNING"; exit }

foreach ($p in $procs) {
  "=== PID $($p.Id) MainWindowTitle='$($p.MainWindowTitle)'"
  $script:rows = @()
  [W]::EnumWindows({ param($h,$l)
      $pid2 = 0; [void][W]::GetWindowThreadProcessId($h, [ref]$pid2)
      if ($pid2 -eq $p.Id) {
        $vis = [W]::IsWindowVisible($h)
        "WIN hwnd=$h vis=$vis class=$(Cls $h) title='$(Txt $h)'"
        $m = [W]::GetMenu($h)
        if ($m -ne [IntPtr]::Zero) { "  has native menu:"; DumpMenu $m 0 }
        [W]::EnumChildWindows($h, { param($c,$l)
            $ct = Txt $c; $cc = Cls $c
            if ($ct -ne '' -or $cc -match 'Qt|Edit|Button|Static|Combo|List|Tree|Table|msctls') {
              "   CHILD hwnd=$c class=$cc id=$([W]::GetDlgCtrlID($c)) vis=$([W]::IsWindowVisible($c)) text='$ct'"
            }
            return $true
        }, [IntPtr]::Zero) | Out-Null
      }
      return $true
  }, [IntPtr]::Zero) | Out-Null
}

# UI Automation tree (Qt exposes names/roles through MSAA bridge)
"=== UIA"
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $procs[0].Id)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
foreach ($w in $wins) {
  "UIAWIN name='$($w.Current.Name)' class='$($w.Current.ClassName)' type=$($w.Current.ControlType.ProgrammaticName)"
  $all = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  foreach ($e in $all) {
    $c = $e.Current
    "  UIA type=$($c.ControlType.ProgrammaticName) name='$($c.Name)' autoid='$($c.AutomationId)' class='$($c.ClassName)' enabled=$($c.IsEnabled) offscreen=$($c.IsOffscreen)"
  }
}
