# 阶段 1 本地依赖安装。只在本 worktree 内创建虚拟环境与 node_modules，不修改全局环境。
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-common.ps1')

$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$BackendDir = Join-Path $ProjectRoot 'backend'
$FrontendDir = Join-Path $ProjectRoot 'frontend'
$VenvDir = Join-Path $BackendDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$LockFile = Join-Path $BackendDir 'requirements.lock'
$PackageLock = Join-Path $FrontendDir 'package-lock.json'

function Find-Python312 {
    $candidates = New-Object System.Collections.Generic.List[string]

    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyCommand) {
        try {
            $resolved = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($resolved | Select-Object -Last 1))) {
                $candidates.Add(($resolved | Select-Object -Last 1).Trim())
            }
        } catch { }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        try {
            $resolved = & python -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace(($resolved | Select-Object -Last 1))) {
                $candidates.Add(($resolved | Select-Object -Last 1).Trim())
            }
        } catch { }
    }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $version = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and (($version | Select-Object -Last 1).Trim() -eq '3.12')) {
            return $candidate
        }
    }
    return $null
}

$python312 = Find-Python312
if ($null -eq $python312) {
    throw "未找到 Python 3.12。请安装 Python 3.12.x，并确保 py -3.12 或 python 可用。本脚本不会安装或替换全局 Python。"
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Host "创建后端虚拟环境：$VenvDir"
    & $python312 -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "创建后端虚拟环境失败（退出码 $LASTEXITCODE）。"
    }
} else {
    Write-Host "复用已有后端虚拟环境：$VenvDir"
}

$venvVersion = & $VenvPython -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0 -or (($venvVersion | Select-Object -Last 1).Trim() -ne '3.12')) {
    throw "现有虚拟环境不是 Python 3.12。请检查 $VenvDir（不会自动删除）。"
}

Write-Host "安装后端依赖：requirements.lock"
& $VenvPython -m pip install --disable-pip-version-check -r $LockFile
if ($LASTEXITCODE -ne 0) {
    throw "后端依赖安装失败（退出码 $LASTEXITCODE）。请检查网络或本地 pip 缓存后重试。"
}

$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if ($null -eq $nodeCommand) {
    throw "未找到 Node.js。请安装 Node.js 20.19 或更高版本（本项目在 Node 24 上验证）。"
}
$nodeVersion = (& node --version).Trim()
if ($nodeVersion -match '^v(\d+)\.(\d+)\.') {
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    if ($major -lt 20 -or ($major -eq 20 -and $minor -lt 19)) {
        throw "Node.js 版本过低（$nodeVersion）。需要 20.19 或更高版本。"
    }
}

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) { $npmCommand = Get-Command npm -ErrorAction SilentlyContinue }
if ($null -eq $npmCommand) {
    throw "未找到 npm。请检查 Node.js 安装。"
}
if (-not (Test-Path -LiteralPath $PackageLock -PathType Leaf)) {
    throw "未找到前端 package-lock.json，无法进行可复现安装。"
}

Write-Host "安装前端依赖：npm ci"
Push-Location -LiteralPath $FrontendDir
try {
    & $npmCommand.Source ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        throw "前端依赖安装失败（退出码 $LASTEXITCODE）。"
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host "  Python      $(& $VenvPython --version)"
Write-Host "  Node.js     $nodeVersion"
Write-Host "  npm         $(& $npmCommand.Source --version)"
Write-Host ""
Write-Host "下一步："
Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\start-local.ps1`""
