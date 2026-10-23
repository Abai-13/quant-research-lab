# 由 install/start/stop 脚本 dot-source 的共享函数。
# 兼容 Windows PowerShell 5.1 与 PowerShell 7。

Set-StrictMode -Version Latest

function Get-JsonProperty {
    param(
        [object]$Object,
        [string]$Name,
        $Default
    )
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Read-DotEnvFile {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $values }
    foreach ($rawLine in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $line = $rawLine.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        $equals = $line.IndexOf('=')
        if ($equals -lt 1) { continue }
        $key = $line.Substring(0, $equals).Trim()
        $value = $line.Substring($equals + 1).Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        if ($key -match '^[A-Za-z_][A-Za-z0-9_]*$') { $values[$key] = $value }
    }
    return $values
}

function Get-ConfigValue {
    param(
        [hashtable]$DotEnv,
        [string]$Name,
        [string]$Default
    )
    $environmentValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($environmentValue)) { return $environmentValue.Trim() }
    if ($DotEnv.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace($DotEnv[$Name])) {
        return [string]$DotEnv[$Name]
    }
    return $Default
}

function ConvertTo-ValidPort {
    param(
        [string]$Name,
        [string]$Raw
    )
    $port = 0
    if (-not [int]::TryParse($Raw, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw "$Name 不是有效端口：'$Raw'（应为 1-65535 的整数）"
    }
    return $port
}

function Resolve-ProjectPath {
    param(
        [string]$Path,
        [string]$ProjectRoot
    )
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $Path))
}

function Initialize-LocalDirectories {
    param(
        [string]$StateDir,
        [string]$LogDir
    )
    foreach ($directory in @($StateDir, $LogDir)) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }
    }
}

function Test-LoopbackPortAvailable {
    param([int]$Port)
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) { $listener.Stop() }
    }
}

function Test-LoopbackPortOpen {
    param([int]$Port)
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait(500)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        if ($null -ne $client) { $client.Dispose() }
    }
}

function Wait-LoopbackPortOpen {
    param(
        [int]$Port,
        [int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-LoopbackPortOpen -Port $Port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Wait-HttpOk {
    param(
        [string]$Url,
        [int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            # 后端尚未就绪，继续等待。
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Wait-FrontendPageReady {
    param(
        [object]$Record,
        [string]$Url,
        [string]$RequiredContent,
        [int]$TimeoutSeconds
    )
    # 前端就绪 = 记录的前端进程仍存活，且页面根路径返回包含应用根元素的 HTML。
    # 只在返回内容匹配时判为就绪，避免把占用该端口的其他服务（如后端）当成前端。
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-ProcessRecordAlive -Record $Record)) { return 'process_exited' }
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                $content = [string]$response.Content
                if ($content.IndexOf($RequiredContent, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                    return 'ready'
                }
            }
        } catch {
            # 前端页面尚未就绪，继续等待。
        }
        Start-Sleep -Milliseconds 500
    }
    return 'timeout'
}

function Get-CimProcess {
    param([int]$ProcessId)
    return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function New-VerifiedProcessRecord {
    param(
        [string]$Name,
        [System.Diagnostics.Process]$Process,
        [string]$Marker,
        [string]$LogPath,
        [string]$ErrorLogPath
    )
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        $cim = Get-CimProcess -ProcessId $Process.Id
        if ($null -ne $cim) {
            $creationTicks = $null
            try { $creationTicks = [string]$cim.CreationDate.ToUniversalTime().Ticks } catch { $creationTicks = $null }
            if ([string]::IsNullOrWhiteSpace($creationTicks)) {
                throw "无法读取 $Name 进程的创建时间，不能安全记录该进程。"
            }
            return [pscustomobject]@{
                name = $Name
                pid = [int]$Process.Id
                creationTicks = $creationTicks
                marker = $Marker
                executable = [string]$cim.ExecutablePath
                log = $LogPath
                errorLog = $ErrorLogPath
                startedAt = (Get-Date).ToString('o')
            }
        }
        Start-Sleep -Milliseconds 100
    }
    throw "$Name 进程启动后立即退出，未记录运行状态。"
}

function Test-ProcessRecordAlive {
    param([object]$Record)
    $processId = [int](Get-JsonProperty -Object $Record -Name 'pid' -Default 0)
    if ($processId -le 0) { return $false }
    $cim = Get-CimProcess -ProcessId $processId
    if ($null -eq $cim) { return $false }

    $expectedTicks = [string](Get-JsonProperty -Object $Record -Name 'creationTicks' -Default '')
    $actualTicks = ''
    try { $actualTicks = [string]$cim.CreationDate.ToUniversalTime().Ticks } catch { return $false }
    if ([string]::IsNullOrWhiteSpace($expectedTicks) -or $actualTicks -ne $expectedTicks) {
        # PID 已被其他进程复用，不能视为本项目进程。
        return $false
    }

    $marker = [string](Get-JsonProperty -Object $Record -Name 'marker' -Default '')
    $commandLine = [string]$cim.CommandLine
    if ([string]::IsNullOrWhiteSpace($marker)) { return $false }
    if ($commandLine.IndexOf($marker, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $false
    }
    return $true
}

function Get-DescendantProcessIds {
    param([int]$RootProcessId)
    $allProcesses = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue)
    $result = New-Object System.Collections.Generic.List[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    $queue.Enqueue($RootProcessId)
    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($candidate in $allProcesses) {
            if ([int]$candidate.ParentProcessId -eq $parentId) {
                $childId = [int]$candidate.ProcessId
                if (-not $result.Contains($childId)) {
                    $result.Add($childId)
                    $queue.Enqueue($childId)
                }
            }
        }
    }
    return $result.ToArray()
}

function Stop-VerifiedProcessRecord {
    param([object]$Record)
    if (-not (Test-ProcessRecordAlive -Record $Record)) { return 'stale' }

    $processId = [int]$Record.pid
    $children = @(Get-DescendantProcessIds -RootProcessId $processId)
    foreach ($childId in $children) {
        Stop-Process -Id $childId -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return 'stopped' }
        Start-Sleep -Milliseconds 200
    }
    if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return 'stopped' }
    return 'failed'
}

function Enter-StateLock {
    param(
        [string]$StateDir,
        [int]$TimeoutSeconds = 10
    )
    $lockPath = Join-Path $StateDir 'local.lock'
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        try {
            return [System.IO.File]::Open(
                $lockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        } catch [System.IO.IOException] {
            if ((Get-Date) -ge $deadline) {
                throw "无法获取本地运行状态锁，请确认没有同时执行另一个启停脚本。"
            }
            Start-Sleep -Milliseconds 250
        }
    }
}

function Exit-StateLock {
    param([System.IO.FileStream]$Stream)
    if ($null -ne $Stream) { $Stream.Dispose() }
}

function Read-LocalState {
    param(
        [string]$StatePath,
        [string]$ProjectRoot
    )
    $records = @()
    $updatedAt = ''
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        $raw = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8
        if (-not [string]::IsNullOrWhiteSpace($raw)) {
            try {
                $parsed = ConvertFrom-Json -InputObject $raw
            } catch {
                throw "运行状态文件无法解析：$StatePath。请检查该文件；数据库不会受影响。"
            }
            $updatedAt = [string](Get-JsonProperty -Object $parsed -Name 'updatedAt' -Default '')
            $recordedRoot = [string](Get-JsonProperty -Object $parsed -Name 'projectRoot' -Default '')
            if (-not [string]::IsNullOrWhiteSpace($recordedRoot)) {
                $recordedFull = [System.IO.Path]::GetFullPath($recordedRoot)
                $currentFull = [System.IO.Path]::GetFullPath($ProjectRoot)
                if (-not [string]::Equals($recordedFull, $currentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "运行状态文件属于其他项目目录（$recordedRoot），拒绝使用。"
                }
            }
            $rawRecords = Get-JsonProperty -Object $parsed -Name 'processes' -Default @()
            foreach ($item in @($rawRecords)) {
                $processId = Get-JsonProperty -Object $item -Name 'pid' -Default $null
                $name = [string](Get-JsonProperty -Object $item -Name 'name' -Default '')
                $creationTicks = [string](Get-JsonProperty -Object $item -Name 'creationTicks' -Default '')
                $marker = [string](Get-JsonProperty -Object $item -Name 'marker' -Default '')
                if ($null -eq $processId -or [string]::IsNullOrWhiteSpace($name) -or
                    [string]::IsNullOrWhiteSpace($creationTicks) -or [string]::IsNullOrWhiteSpace($marker)) {
                    Write-Warning "忽略一条不完整的运行记录。"
                    continue
                }
                $records += [pscustomobject]@{
                    name = $name
                    pid = [int]$processId
                    creationTicks = $creationTicks
                    marker = $marker
                    executable = [string](Get-JsonProperty -Object $item -Name 'executable' -Default '')
                    log = [string](Get-JsonProperty -Object $item -Name 'log' -Default '')
                    errorLog = [string](Get-JsonProperty -Object $item -Name 'errorLog' -Default '')
                    startedAt = [string](Get-JsonProperty -Object $item -Name 'startedAt' -Default '')
                }
            }
        }
    }
    return [pscustomobject]@{
        projectRoot = $ProjectRoot
        updatedAt = $updatedAt
        processes = @($records)
    }
}

function Write-LocalState {
    param(
        [string]$StatePath,
        [object]$State
    )
    $State.updatedAt = (Get-Date).ToString('o')
    $json = $State | ConvertTo-Json -Depth 6
    $tempPath = "$StatePath.tmp"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tempPath, $json, $encoding)
    Move-Item -LiteralPath $tempPath -Destination $StatePath -Force
}

function Remove-LocalState {
    param([string]$StatePath)
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        Remove-Item -LiteralPath $StatePath -Force
    }
}

function Get-LogTail {
    param(
        [string]$Path,
        [int]$Lines = 20
    )
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue
    }
}
