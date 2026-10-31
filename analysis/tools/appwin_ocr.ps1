# Capture+OCR all visible windows belonging to the makemkv process, tagged by window.
param([string]$OutDir = "$env:TEMP", [double]$Scale = 2.0)
Add-Type -AssemblyName System.Drawing, System.Runtime.WindowsRuntime
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class AW {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr c);
  public struct RECT { public int L,T,R,B; }
}
"@
try { [void][AW]::SetProcessDpiAwarenessContext([IntPtr]-4) } catch { [void][AW]::SetProcessDPIAware() }
$p = Get-Process makemkv -ErrorAction Stop | Select-Object -First 1
$wins = New-Object System.Collections.ArrayList
[AW]::EnumWindows({ param($h,$l)
  $pp=0; [void][AW]::GetWindowThreadProcessId($h,[ref]$pp)
  if ($pp -eq $p.Id -and [AW]::IsWindowVisible($h)) {
    $sb = New-Object System.Text.StringBuilder 512; [void][AW]::GetWindowTextW($h,$sb,512)
    $cb = New-Object System.Text.StringBuilder 256; [void][AW]::GetClassNameW($h,$cb,256)
    $r = New-Object AW+RECT; [void][AW]::GetWindowRect($h,[ref]$r)
    if (($r.R-$r.L) -gt 30 -and ($r.B-$r.T) -gt 20) {
      [void]$script:wins.Add([pscustomobject]@{h=$h;t=$sb.ToString();c=$cb.ToString();l=$r.L;tp=$r.T;w=$r.R-$r.L;h2=$r.B-$r.T})
    }
  }
  return $true }, [IntPtr]::Zero) | Out-Null

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op,$t2) { $task = $asTask.MakeGenericMethod($t2).Invoke($null,@($op)); $task.Wait(-1)|Out-Null; $task.Result }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()

$i = 0
foreach ($w in $wins) {
  $i++
  $bmp = New-Object System.Drawing.Bitmap $w.w, $w.h2
  $g = [System.Drawing.Graphics]::FromImage($bmp); $hdc = $g.GetHdc()
  [void][AW]::PrintWindow($w.h, $hdc, 2); $g.ReleaseHdc($hdc); $g.Dispose()
  $big = New-Object System.Drawing.Bitmap ([int]($w.w*$Scale)), ([int]($w.h2*$Scale))
  $g2 = [System.Drawing.Graphics]::FromImage($big)
  $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $g2.DrawImage($bmp, 0, 0, $big.Width, $big.Height); $g2.Dispose()
  $out = Join-Path $OutDir ("win{0}.png" -f $i)
  $big.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  "WINDOW #{0} '{1}' class={2} at ({3},{4}) size={5}x{6} png={7}" -f $i, $w.t, $w.c, $w.l, $w.tp, $w.w, $w.h2, $out
  $full = (Resolve-Path $out).Path
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
  $st = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $sbmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $res = Await ($engine.RecognizeAsync($sbmp)) ([Windows.Media.Ocr.OcrResult])
  foreach ($line in $res.Lines) {
    $ws = @($line.Words)
    $lx = [int]($ws[0].BoundingRect.X / $Scale) + $w.l; $ly = [int]($ws[0].BoundingRect.Y / $Scale) + $w.tp
    "   [x={0,4} y={1,4}] {2}" -f $lx, $ly, (($ws | ForEach-Object { $_.Text }) -join ' ')
  }
}
