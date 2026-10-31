# Capture window region + margin from the screen and OCR it (for popups/menus/dialogs).
param(
  [string]$Title = "MakeMKV",
  [int]$Margin = 420,
  [string]$Out = "$env:TEMP\region.png",
  [double]$Scale = 2.0
)
Add-Type -AssemblyName System.Drawing, System.Windows.Forms, System.Runtime.WindowsRuntime
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class RC {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr c);
  public struct RECT { public int L,T,R,B; }
}
"@
try { [void][RC]::SetProcessDpiAwarenessContext([IntPtr]-4) } catch { [void][RC]::SetProcessDPIAware() }
$p = Get-Process makemkv -ErrorAction Stop | Select-Object -First 1
$wins = New-Object System.Collections.ArrayList
[RC]::EnumWindows({ param($h,$l)
  $pp=0; [void][RC]::GetWindowThreadProcessId($h,[ref]$pp)
  if ($pp -eq $p.Id -and [RC]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 512; [void][RC]::GetWindowTextW($h,$sb,512)
    [void]$script:wins.Add([pscustomobject]@{h=$h;t=$sb.ToString()})
  }
  return $true }, [IntPtr]::Zero) | Out-Null
$t = $wins | Where-Object { $_.t -match $Title } | Select-Object -First 1
if (-not $t) { "no window"; exit 1 }
$r = New-Object RC+RECT; [void][RC]::GetWindowRect($t.h,[ref]$r)
$sw = [System.Windows.Forms.SystemInformation]::VirtualScreen.Width
$sh2 = [System.Windows.Forms.SystemInformation]::VirtualScreen.Height
$x = [Math]::Max(0, $r.L - 10); $y = [Math]::Max(0, $r.T - 10)
$w = [Math]::Min($sw - $x, ($r.R - $r.L) + $Margin); $h = [Math]::Min($sh2 - $y, ($r.B - $r.T) + 260)
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($x, $y, 0, 0, $bmp.Size); $g.Dispose()
$big = New-Object System.Drawing.Bitmap ([int]($w*$Scale)), ([int]($h*$Scale))
$g2 = [System.Drawing.Graphics]::FromImage($big)
$g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g2.DrawImage($bmp, 0, 0, $big.Width, $big.Height); $g2.Dispose()
$big.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
"CAPTURED title='$($t.t)' origin=($x,$y) size=${w}x${h} -> $Out"

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op,$t2) { $task = $asTask.MakeGenericMethod($t2).Invoke($null,@($op)); $task.Wait(-1)|Out-Null; $task.Result }
$full = (Resolve-Path $Out).Path -replace '/', '\'
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
$st = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
$sbmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$res = Await ($engine.RecognizeAsync($sbmp)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $res.Lines) {
  $ws = @($line.Words)
  $lx = [int]($ws[0].BoundingRect.X / $Scale) + $x; $ly = [int]($ws[0].BoundingRect.Y / $Scale) + $y
  "[x={0,4} y={1,4}] {2}" -f $lx, $ly, (($ws | ForEach-Object { $_.Text }) -join ' ')
}
