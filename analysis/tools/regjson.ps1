param([string]$Out = "$env:TEMP\reg.json")
$k = Get-Item 'HKCU:\Software\MakeMKV' -ErrorAction Stop
$vals = @{}
$it = Get-ItemProperty $k.PSPath
$it.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object { $vals[$_.Name] = $_.Value }
[pscustomobject]@{ key = $k.Name; values = $vals } | ConvertTo-Json -Depth 3 | Set-Content -Encoding UTF8 $Out
"wrote $Out : $($vals.Count) values"
$vals.GetEnumerator() | Sort-Object Name | ForEach-Object { "  {0} = {1}" -f $_.Key, $_.Value }
