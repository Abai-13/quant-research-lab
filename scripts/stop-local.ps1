# 停止阶段 1 本地服务。只停止启动脚本记录且能核验身份的进程。
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-common.ps1')

$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$dotEnv = Read-DotEnvFile -Path (Join-Path $ProjectRoot '.env')
$runtimeDir = Resolve-ProjectPath -Path (Get-ConfigValue -DotEnv $dotEnv -Name 'APP_RUNTIME_DIR' -Default '.runtime') -ProjectRoot $ProjectRoot
$stateDir = Join-Path $runtimeDir 'state'
$statePath = Join-Path $stateDir 'processes.json'

$backendPort = ConvertTo-ValidPort -Name 'APP_BACKEND_PORT' -Raw (Get-ConfigValue -DotEnv $dotEnv -Name 'APP_BACKEND_PORT' -Default '8000')
$frontendPort = ConvertTo-ValidPort -Name 'APP_FRONTEND_PORT' -Raw (Get-ConfigValue -DotEnv $dotEnv -Name 'APP_FRONTEND_PORT' -Default '5173')

Initialize-LocalDirectories -StateDir $stateDir -LogDir (Join-Path $runtimeDir 'logs')

$lockStream = $null
try {
    $lockStream = Enter-StateLock -StateDir $stateDir
    $state = Read-LocalState -StatePath $statePath -ProjectRoot $ProjectRoot
    $records = @($state.processes)
    $remaining = @()
    $stopFailures = 0
    $stoppedCount = 0
    $staleCount = 0

    # 先停前端，再停后端，避免页面在后端停止后继续产生请求。
    $orderedRecords = @($records | Sort-Object -Property @{ Expression = { if ($_.name -eq 'backend') { 1 } else { 0 } } })

    foreach ($record in $orderedRecords) {
        if (-not (Test-ProcessRecordAlive -Record $record)) {
            Write-Host "跳过过期记录：$($record.name) (PID $($record.pid))，进程不存在或 PID 已被复用。" -ForegroundColor Yellow
            $staleCount++
            continue
        }
        $result = Stop-VerifiedProcessRecord -Record $record
        if ($result -eq 'stopped') {
            Write-Host "已停止 $($record.name) (PID $($record.pid))。" -ForegroundColor Green
            $stoppedCount++
        } elseif ($result -eq 'stale') {
            Write-Host "跳过已结束的 $($record.name) (PID $($record.pid))。" -ForegroundColor Yellow
            $staleCount++
        } else {
            Write-Host "无法停止 $($record.name) (PID $($record.pid))；记录已保留，请手动检查该进程。" -ForegroundColor Red
            $remaining += $record
            $stopFailures++
        }
    }

    if ($remaining.Count -gt 0) {
        $nextState = [pscustomobject]@{
            projectRoot = $ProjectRoot
            updatedAt = ''
            processes = @($remaining)
        }
        Write-LocalState -StatePath $statePath -State $nextState
    } else {
        Remove-LocalState -StatePath $statePath
    }

    if ($records.Count -eq 0) {
        Write-Host "没有记录到由本脚本启动的进程。"
        if (Test-LoopbackPortOpen -Port $backendPort) {
            Write-Warning "端口 $backendPort 仍在监听，但没有本项目的运行记录；不会终止未确认的进程。"
        }
        if (Test-LoopbackPortOpen -Port $frontendPort) {
            Write-Warning "端口 $frontendPort 仍在监听，但没有本项目的运行记录；不会终止未确认的进程。"
        }
    } else {
        Write-Host "停止完成：已停止 $stoppedCount 个，跳过过期记录 $staleCount 个，失败 $stopFailures 个。"
    }

    if ($stopFailures -gt 0) { exit 1 }
} finally {
    Exit-StateLock -Stream $lockStream
}
