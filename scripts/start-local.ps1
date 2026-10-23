# 启动阶段 1 本地服务（后端 + 前端）。
# 特性：从任意目录调用；端口占用时明确失败；记录并复用自己启动的进程。
[CmdletBinding()]
param(
    [int]$BackendPort = 0,
    [int]$FrontendPort = 0
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-common.ps1')

$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$BackendDir = Join-Path $ProjectRoot 'backend'
$FrontendDir = Join-Path $ProjectRoot 'frontend'
$VenvPython = Join-Path $BackendDir '.venv\Scripts\python.exe'
$ViteScript = Join-Path $FrontendDir 'node_modules\vite\bin\vite.js'

$dotEnv = Read-DotEnvFile -Path (Join-Path $ProjectRoot '.env')

$configuredBackendPort = ConvertTo-ValidPort -Name 'APP_BACKEND_PORT' -Raw (Get-ConfigValue -DotEnv $dotEnv -Name 'APP_BACKEND_PORT' -Default '8000')
$configuredFrontendPort = ConvertTo-ValidPort -Name 'APP_FRONTEND_PORT' -Raw (Get-ConfigValue -DotEnv $dotEnv -Name 'APP_FRONTEND_PORT' -Default '5173')
if ($BackendPort -ne 0 -and ($BackendPort -lt 1 -or $BackendPort -gt 65535)) {
    throw "-BackendPort 必须是 1-65535 之间的整数。"
}
if ($FrontendPort -ne 0 -and ($FrontendPort -lt 1 -or $FrontendPort -gt 65535)) {
    throw "-FrontendPort 必须是 1-65535 之间的整数。"
}
$effectiveBackendPort = if ($BackendPort -ne 0) { $BackendPort } else { $configuredBackendPort }
$effectiveFrontendPort = if ($FrontendPort -ne 0) { $FrontendPort } else { $configuredFrontendPort }

# 后端与前端必须使用不同端口。同端口时前端无法绑定，且仅探测端口会把后端误判为前端就绪。
if ($effectiveBackendPort -eq $effectiveFrontendPort) {
    throw "APP_BACKEND_PORT 与 APP_FRONTEND_PORT 不能相同（当前均为 $effectiveBackendPort）。请为前端或后端配置不同端口。"
}

$backendHost = Get-ConfigValue -DotEnv $dotEnv -Name 'APP_BACKEND_HOST' -Default '127.0.0.1'
# 把本次生效的端口传给子进程，确保 Vite 的 /api 代理与后端实际端口一致
# （即使通过 -BackendPort / -FrontendPort 临时覆盖）。
$previousBackendPortEnv = $env:APP_BACKEND_PORT
$previousFrontendPortEnv = $env:APP_FRONTEND_PORT
$env:APP_BACKEND_PORT = [string]$effectiveBackendPort
$env:APP_FRONTEND_PORT = [string]$effectiveFrontendPort
if ([string]::IsNullOrWhiteSpace($backendHost)) { throw 'APP_BACKEND_HOST 不能为空。' }
if (@('127.0.0.1', 'localhost', '::1') -notcontains $backendHost) {
    Write-Warning "APP_BACKEND_HOST=$backendHost 不是回环地址。本阶段没有登录与 HTTPS，请不要向公网开放。"
}

$runtimeDir = Resolve-ProjectPath -Path (Get-ConfigValue -DotEnv $dotEnv -Name 'APP_RUNTIME_DIR' -Default '.runtime') -ProjectRoot $ProjectRoot
$logDir = Resolve-ProjectPath -Path (Get-ConfigValue -DotEnv $dotEnv -Name 'APP_LOG_DIR' -Default (Join-Path $runtimeDir 'logs')) -ProjectRoot $ProjectRoot
$stateDir = Join-Path $runtimeDir 'state'
$statePath = Join-Path $stateDir 'processes.json'
$backendDbPath = Resolve-ProjectPath -Path (Get-ConfigValue -DotEnv $dotEnv -Name 'APP_DB_PATH' -Default (Join-Path $runtimeDir 'data\app.db')) -ProjectRoot $ProjectRoot

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw "后端虚拟环境未安装。请先运行：powershell -NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\install-local.ps1`""
}
if (-not (Test-Path -LiteralPath $ViteScript -PathType Leaf)) {
    throw "前端依赖未安装。请先运行：powershell -NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\install-local.ps1`""
}

$probeHost = $backendHost
if ($probeHost -eq '0.0.0.0' -or $probeHost -eq '::') { $probeHost = '127.0.0.1' }
$urlHost = if ($probeHost.Contains(':')) { "[$probeHost]" } else { $probeHost }
$healthUrl = "http://$urlHost`:$effectiveBackendPort/api/health"

Initialize-LocalDirectories -StateDir $stateDir -LogDir $logDir

$lockStream = $null
$lockAcquired = $false
$stateLoaded = $false
$startedRecords = @()
$retainedRecords = @()
$backendReused = $false
$frontendReused = $false

try {
    $lockStream = Enter-StateLock -StateDir $stateDir
    $lockAcquired = $true

    $state = Read-LocalState -StatePath $statePath -ProjectRoot $ProjectRoot
    $stateLoaded = $true
    $backendRecord = $null
    $frontendRecord = $null

    foreach ($record in @($state.processes)) {
        if (Test-ProcessRecordAlive -Record $record) {
            $retainedRecords += $record
            if ($record.name -eq 'backend' -and $null -eq $backendRecord) {
                $backendRecord = $record
            } elseif ($record.name -eq 'frontend' -and $null -eq $frontendRecord) {
                $frontendRecord = $record
            }
        } else {
            Write-Host "清理过期运行记录：$($record.name) (PID $($record.pid))，进程不存在或 PID 已被复用。" -ForegroundColor Yellow
        }
    }

    if ($null -eq $backendRecord) {
        if (-not (Test-LoopbackPortAvailable -Port $effectiveBackendPort)) {
            throw "端口 $effectiveBackendPort 已被占用。请停止占用该端口的程序，或在 .env 中修改 APP_BACKEND_PORT。脚本不会自动终止其他进程。"
        }
    } else {
        $backendReused = $true
        Write-Host "后端已在运行（PID $($backendRecord.pid)），不重复启动。"
    }

    if ($null -eq $frontendRecord) {
        if (-not (Test-LoopbackPortAvailable -Port $effectiveFrontendPort)) {
            throw "端口 $effectiveFrontendPort 已被占用。请停止占用该端口的程序，或在 .env 中修改 APP_FRONTEND_PORT。脚本不会自动终止其他进程。"
        }
    } else {
        $frontendReused = $true
        Write-Host "前端已在运行（PID $($frontendRecord.pid)），不重复启动。"
    }

    if ($null -eq $backendRecord) {
        $backendOut = Join-Path $logDir 'backend.out.log'
        $backendErr = Join-Path $logDir 'backend.err.log'
        Write-Host "启动后端：http://$urlHost`:$effectiveBackendPort"
        $backendArguments = '-m uvicorn app.asgi:app --host ' + $backendHost + ' --port ' + $effectiveBackendPort + ' --log-level info'
        $backendProcess = Start-Process -FilePath $VenvPython -ArgumentList $backendArguments -WorkingDirectory $BackendDir -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -WindowStyle Hidden -PassThru
        $backendRecord = New-VerifiedProcessRecord -Name 'backend' -Process $backendProcess -Marker $VenvPython -LogPath $backendOut -ErrorLogPath $backendErr
        $startedRecords += $backendRecord

        # 每启动一个服务就落盘，避免脚本被中断后丢失自己创建的进程记录。
        $stateForWrite = [pscustomobject]@{
            projectRoot = $ProjectRoot
            updatedAt = ''
            processes = @($retainedRecords + $startedRecords)
        }
        Write-LocalState -StatePath $statePath -State $stateForWrite
    }

    if (-not (Wait-HttpOk -Url $healthUrl -TimeoutSeconds 30)) {
        $tail = @()
        $tail += Get-LogTail -Path (Join-Path $logDir 'backend.err.log') -Lines 20
        $tail += Get-LogTail -Path (Join-Path $logDir 'backend.out.log') -Lines 20
        throw ("后端在 30 秒内未通过健康检查。日志目录：$logDir`n" + ($tail -join "`n"))
    }

    if ($null -eq $frontendRecord) {
        $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
        if ($null -eq $nodeCommand) { throw '未找到 node 命令，无法启动前端。' }
        $nodeExe = $nodeCommand.Source
        $frontendOut = Join-Path $logDir 'frontend.out.log'
        $frontendErr = Join-Path $logDir 'frontend.err.log'
        Write-Host "启动前端：http://127.0.0.1:$effectiveFrontendPort"
        $frontendArguments = '"' + $ViteScript + '" --host 127.0.0.1 --port ' + $effectiveFrontendPort + ' --strictPort'
        $frontendProcess = Start-Process -FilePath $nodeExe -ArgumentList $frontendArguments -WorkingDirectory $FrontendDir -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr -WindowStyle Hidden -PassThru
        $frontendRecord = New-VerifiedProcessRecord -Name 'frontend' -Process $frontendProcess -Marker $ViteScript -LogPath $frontendOut -ErrorLogPath $frontendErr
        $startedRecords += $frontendRecord

        # 就绪检查同时要求：记录的前端进程仍存活，且前端端口返回包含应用根元素的 HTML。
        # 仅探测端口会把恰好占用同一端口的后端误当成前端。
        $frontendUrl = "http://127.0.0.1:$effectiveFrontendPort/"
        $frontendReadiness = Wait-FrontendPageReady -Record $frontendRecord -Url $frontendUrl -RequiredContent '<div id="root"></div>' -TimeoutSeconds 60
        if ($frontendReadiness -ne 'ready') {
            $tail = @()
            $tail += Get-LogTail -Path $frontendErr -Lines 20
            $tail += Get-LogTail -Path $frontendOut -Lines 20
            $reason = if ($frontendReadiness -eq 'process_exited') {
                "前端进程 (PID $($frontendRecord.pid)) 已退出"
            } else {
                "前端在 60 秒内未通过页面就绪检查（端口 $effectiveFrontendPort）"
            }
            throw ("$reason。日志目录：$logDir`n" + ($tail -join "`n"))
        }
    }

    $stateForWrite = [pscustomobject]@{
        projectRoot = $ProjectRoot
        updatedAt = ''
        processes = @($retainedRecords + $startedRecords)
    }
    Write-LocalState -StatePath $statePath -State $stateForWrite

    Write-Host ""
    Write-Host "本地服务已就绪。" -ForegroundColor Green
    Write-Host "  页面        http://127.0.0.1:$effectiveFrontendPort"
    Write-Host "  健康检查    $healthUrl"
    Write-Host "  数据库      $backendDbPath"
    Write-Host "  日志目录    $logDir"
    Write-Host "  后端复用    $backendReused；前端复用 $frontendReused"
    Write-Host ""
    Write-Host "停止服务："
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\stop-local.ps1`""
} catch {
    $failureMessage = $_.Exception.Message
    Write-Host ""
    Write-Host "启动失败：$failureMessage" -ForegroundColor Red

    if ($startedRecords.Count -gt 0) {
        foreach ($record in $startedRecords) {
            $result = Stop-VerifiedProcessRecord -Record $record
            if ($result -eq 'stopped') {
                Write-Host "已停止本次启动的 $($record.name) 进程 (PID $($record.pid))。"
            } elseif ($result -eq 'stale') {
                Write-Host "本次启动的 $($record.name) 进程 (PID $($record.pid)) 已自行退出，无需停止。"
            } else {
                Write-Host "无法确认 $($record.name) 进程 (PID $($record.pid)) 已停止，记录已保留；请运行 stop-local.ps1 处理。" -ForegroundColor Red
            }
        }
    }

    if ($lockAcquired -and $stateLoaded) {
        $keep = @()
        foreach ($record in $retainedRecords) { $keep += $record }
        foreach ($record in $startedRecords) {
            if (Test-ProcessRecordAlive -Record $record) { $keep += $record }
        }
        $failedState = [pscustomobject]@{
            projectRoot = $ProjectRoot
            updatedAt = ''
            processes = @($keep)
        }
        Write-LocalState -StatePath $statePath -State $failedState
    }
    exit 1
} finally {
    Exit-StateLock -Stream $lockStream
    if ($null -eq $previousBackendPortEnv) {
        Remove-Item Env:APP_BACKEND_PORT -ErrorAction SilentlyContinue
    } else {
        $env:APP_BACKEND_PORT = $previousBackendPortEnv
    }
    if ($null -eq $previousFrontendPortEnv) {
        Remove-Item Env:APP_FRONTEND_PORT -ErrorAction SilentlyContinue
    } else {
        $env:APP_FRONTEND_PORT = $previousFrontendPortEnv
    }
}
