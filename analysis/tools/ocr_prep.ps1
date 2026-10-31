# Crop + grayscale + contrast stretch + nearest upscale, then OCR.
param(
  [Parameter(Mandatory=$true)][string]$In,
  [int]$X = 0, [int]$Y = 0, [int]$W = 0, [int]$H = 0,
  [int]$Zoom = 6,
  [string]$Out = "$env:TEMP\prep.png"
)
Add-Type -AssemblyName System.Drawing, System.Runtime.WindowsRuntime
$src = [System.Drawing.Image]::FromFile((Resolve-Path $In).Path)
if ($W -le 0) { $W = $src.Width - $X }
if ($H -le 0) { $H = $src.Height - $Y }
$crop = New-Object System.Drawing.Bitmap $W, $H
$g = [System.Drawing.Graphics]::FromImage($crop)
$g.DrawImage($src, (New-Object System.Drawing.Rectangle 0,0,$W,$H), (New-Object System.Drawing.Rectangle $X,$Y,$W,$H), [System.Drawing.GraphicsUnit]::Pixel)
$g.Dispose(); $src.Dispose()

# grayscale + contrast stretch into a new bitmap
$gray = New-Object System.Drawing.Bitmap $W, $H
for ($yy=0; $yy -lt $H; $yy++) {
  for ($xx=0; $xx -lt $W; $xx++) {
    $c = $crop.GetPixel($xx,$yy)
    $l = [int](0.299*$c.R + 0.587*$c.G + 0.114*$c.B)
    $v = if ($l -lt 110) { 0 } elseif ($l -gt 170) { 255 } else { 255 * ($l - 110) / 60 }
    $gray.SetPixel($xx,$yy,[System.Drawing.Color]::FromArgb(255,[int]$v,[int]$v,[int]$v))
  }
}
$big = New-Object System.Drawing.Bitmap ($W*$Zoom), ($H*$Zoom)
$g2 = [System.Drawing.Graphics]::FromImage($big)
$g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
$g2.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
$g2.DrawImage($gray, 0, 0, $big.Width, $big.Height); $g2.Dispose()
$big.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)

$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op,$t2) { $task = $asTask.MakeGenericMethod($t2).Invoke($null,@($op)); $task.Wait(-1)|Out-Null; $task.Result }
$full = (Resolve-Path $Out).Path
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
$st = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
$sbmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$res = Await ($engine.RecognizeAsync($sbmp)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $res.Lines) {
  $ws = @($line.Words)
  $lx = [int]($ws[0].BoundingRect.X / $Zoom) + $X; $ly = [int]($ws[0].BoundingRect.Y / $Zoom) + $Y
  "[x={0,4} y={1,4}] {2}" -f $lx, $ly, (($ws | ForEach-Object { $_.Text }) -join ' ')
}
