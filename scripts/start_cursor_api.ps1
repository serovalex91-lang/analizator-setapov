$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
try {
  Set-Location ..
  if (Test-Path .\.env) { Get-Content .\.env | % { if ($_ -and ($_ -notmatch '^[#;]') -and ($_ -match '=')) { $kv = $_ -split '=',2; Set-Item -Path Env:$($kv[0].Trim()) -Value $kv[1].Trim() } } }
  if (Test-Path .\ai.env) { Get-Content .\ai.env | % { if ($_ -and ($_ -notmatch '^[#;]') -and ($_ -match '=')) { $kv = $_ -split '=',2; Set-Item -Path Env:$($kv[0].Trim()) -Value $kv[1].Trim() } } }
  py -3 -m uvicorn cursor_api:app --host 127.0.0.1 --port 8010 --log-level info
}
finally {
  Pop-Location
}


