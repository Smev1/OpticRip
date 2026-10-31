# Activate the MakeMKV window by title and send keystrokes.
param(
  [string]$Title = "MakeMKV",
  [string]$Keys = "",
  [int]$SleepMs = 600
)
$sh = New-Object -ComObject WScript.Shell
Add-Type -AssemblyName System.Windows.Forms
# AppActivate accepts a partial title; retry a few times
$ok = $false
for ($i = 0; $i -lt 5; $i++) {
  try { if ($sh.AppActivate($Title)) { $ok = $true; break } } catch {}
  Start-Sleep -Milliseconds 300
}
if (-not $ok) { "AppActivate failed for '$Title'"; exit 1 }
Start-Sleep -Milliseconds 250
if ($Keys) { [System.Windows.Forms.SendKeys]::SendWait($Keys); "sent $Keys" }
Start-Sleep -Milliseconds $SleepMs
