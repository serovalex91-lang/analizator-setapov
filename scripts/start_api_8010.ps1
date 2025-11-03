$ErrorActionPreference = 'Stop'
cd "C:\Users\Алексей\Desktop\trading_bot_github"
.\.win-venv\Scripts\Activate.ps1
# важно: чтобы модуль app нашёлся
$env:PYTHONPATH = "intraday-levels-taapi"

function Set-EnvFromDotEnv($path){
  if (!(Test-Path $path)) { return }
  Get-Content $path | ForEach-Object {
    $line = $_.Trim()
    if (!$line -or $line.StartsWith("#")) { return }
    $i = $line.IndexOf("="); if ($i -lt 1) { return }
    $k = $line.Substring(0,$i).Trim()
    $v = $line.Substring($i+1).Trim()
    Set-Item -Path Env:$k -Value $v
  }
}

# подхватим переменные
Set-EnvFromDotEnv ".\intraday-levels-taapi\.env"
Set-EnvFromDotEnv ".\.env"

# убьём старый uvicorn, если есть
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*uvicorn*" } | Stop-Process -Force -ErrorAction SilentlyContinue

# старт
python -m uvicorn app.main_v2:app --host 127.0.0.1 --port 8010 --app-dir intraday-levels-taapi\app --log-level info
