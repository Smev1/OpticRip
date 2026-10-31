# Capture a window with PrintWindow, upscale, OCR, and print text with window-relative coords.
param(
  [string]$Title = "",     # regex on window title
  [string]$Out = "$env:TEMP\shot.png",
  [double]$Scale = 2.0
)
Add-Type -AssemblyName System.Drawing, System.Runtime.WindowsRuntime
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class CW {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr c);
  public struct RECT { public int L, T, R, B; }
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
[void][CW]::SetForegroundWindow($target.h); Start-Sleep -Milliseconds 350
$r = New-Object CW+RECT; [void][CW]::GetWindowRect($target.h,[ref]$r)
$w = $r.R - $r.L; $h2 = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap $w, $h2
$g = [System.Drawing.Graphics]::FromImage($bmp); $hdc = $g.GetHdc()
[void][CW]::PrintWindow($target.h, $hdc, 2); $g.ReleaseHdc($hdc); $g.Dispose()
$big = New-Object System.Drawing.Bitmap ([int]($w*$Scale)), ([int]($h2*$Scale))
$g2 = [System.Drawing.Graphics]::FromImage($big)
$g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g2.DrawImage($bmp, 0, 0, $big.Width, $big.Height); $g2.Dispose()
$big.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
"CAPTURED '$($target.t)' ${w}x${h2} -> $Out"

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op,$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null,@($op)); $task.Wait(-1)|Out-Null; $task.Result }
$full = (Resolve-Path $Out).Path -replace '/', '\'
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
$st = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
$sbmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$res = Await ($engine.RecognizeAsync($sbmp)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $res.Lines) {
  $ws = @($line.Words)
  $x = [int]($ws[0].BoundingRect.X / $Scale); $y = [int]($ws[0].BoundingRect.Y / $Scale)
  "[x={0,4} y={1,4}] {2}" -f $x, $y, (($ws | ForEach-Object { $_.Text }) -join ' ')
}
