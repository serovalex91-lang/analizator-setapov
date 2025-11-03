 $ErrorActionPreference = "Stop"

 # Определяем директорию скрипта и корень проекта (на уровень выше)
 $ScriptDir = Split-Path -Parent $PSCommandPath
 $ProjectRoot = Join-Path $ScriptDir ".."

 Push-Location $ProjectRoot
 try {
     # 1) Загрузка env (.env и ai.env), без перезаписи уже заданных переменных
     $envFiles = @()
     if (Test-Path ".\ai.env") { $envFiles += ".\ai.env" }
     if (Test-Path ".\.env")   { $envFiles += ".\.env" }

     foreach ($envFile in $envFiles) {
         Write-Host "Loading $envFile"
         Get-Content $envFile | ForEach-Object {
             $line = $_.Trim()
             if ($line -eq "" -or $line.StartsWith("#") -or $line.StartsWith(";")) { return }
             if ($line -like "export *") { $line = $line.Substring(7).Trim() }
             $lineNoComment = ($line -split "\s+#", 2)[0]
             $kv = $lineNoComment -split "=", 2
             if ($kv.Count -ne 2) { return }
             $key = $kv[0].Trim()
             $val = $kv[1].Trim().Trim("'").Trim('"')
             if ([string]::IsNullOrWhiteSpace($val)) { return }
             $current = [Environment]::GetEnvironmentVariable($key, 'Process')
             if ([string]::IsNullOrWhiteSpace($current)) {
                 Set-Item -Path ("Env:" + $key) -Value $val
                 [Environment]::SetEnvironmentVariable($key, $val, 'Process')
             }
         }
     }

     # 2) Путь к venv Python (без активации сред)
     $VenvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
     $UseVenv = Test-Path $VenvPython

     # 3) Установка зависимостей, фильтруем uvloop кроссплатформенно
     $ReqOrig = Join-Path $ProjectRoot "requirements.txt"
     $ReqTmp  = Join-Path $ProjectRoot "requirements.nouvloop.txt"

     if (Test-Path $ReqOrig) {
         Get-Content $ReqOrig | Select-String -NotMatch "^uvloop" | Set-Content -NoNewline:$false $ReqTmp
         if ($UseVenv) {
             & $VenvPython -m pip install -U pip setuptools wheel
             & $VenvPython -m pip install -r $ReqTmp
         } else {
             Write-Warning "venv не найден, используем системный Python (py -3)"
             py -3 -m pip install -U pip setuptools wheel
             py -3 -m pip install -r $ReqTmp
         }
         Remove-Item $ReqTmp -Force -ErrorAction SilentlyContinue
     } else {
         Write-Warning "requirements.txt не найден — пропускаю установку пакетов"
     }

     # 4) Запуск бота
     $BotFile = Join-Path $ProjectRoot "ai_agent_bot.py"
    if (-not (Test-Path $BotFile)) {
        throw ("File not found: " + $BotFile)
    }

     if ($UseVenv) {
         & $VenvPython $BotFile
     } else {
         py -3 $BotFile
     }
 }
 finally {
     Pop-Location
 }


