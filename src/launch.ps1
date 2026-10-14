# Windows launcher. Mirrors wxMoments: the root BAT only delegates here.
# PowerShell 5.1 compatible; interactive console messages are English only.
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$RuntimeDir = Join-Path $ProjectRoot 'runtime'
$VenvDir = Join-Path $RuntimeDir '.venv'
$PythonExe = Join-Path $VenvDir 'Scripts\python.exe'
$InstallLog = Join-Path $RuntimeDir 'install.log'
$ServerLog = Join-Path $RuntimeDir 'server.log'
$ServerErrorLog = Join-Path $RuntimeDir 'server.err.log'
$Requirements = Join-Path $ProjectRoot 'config\requirements.txt'
$MediaRequirements = Join-Path $ProjectRoot 'config\zhihu-media-requirements.txt'
$DependencyStamp = Join-Path $RuntimeDir 'requirements.sha256'
$MediaStamp = Join-Path $RuntimeDir 'zhihu-requirements.sha256'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$ServiceName = 'BlogNotes'

function Show-StartupError([string]$Message) {
    Write-Host ('ERROR: ' + $Message) -ForegroundColor Red
    Write-Host 'See runtime/install.log and runtime/server.log for details.'
    try {
        $Lines = @($Message)
        foreach ($File in @($InstallLog, $ServerLog, $ServerErrorLog)) {
            if (Test-Path -LiteralPath $File) {
                $Lines += ('--- ' + [IO.Path]::GetFileName($File) + ' ---')
                $Lines += @(Get-Content -LiteralPath $File -Tail 90)
            }
        }
        $Body = [Net.WebUtility]::HtmlEncode(($Lines -join "`n"))
        $Html = @"
<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BlogNotes - Startup failed</title><style>
:root{color-scheme:light dark}body{max-width:860px;margin:36px auto;padding:0 20px;font:15px/1.6 system-ui,sans-serif}
pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:16px;border:1px solid #8885;border-radius:8px}
</style><h1>BlogNotes could not start</h1><p>Fix the issue and run <code>run.bat</code> again.</p>
<pre>$Body</pre></html>
"@
        $ErrorPage = Join-Path $RuntimeDir 'startup-error.html'
        [IO.File]::WriteAllText($ErrorPage, $Html, (New-Object Text.UTF8Encoding($false)))
        Start-Process -FilePath $ErrorPage
    } catch {
        Write-Host 'Unable to open the startup error report.'
    }
}

function Invoke-Logged([scriptblock]$Task, [string]$Label) {
    $Previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Task *>> $InstallLog
        $Code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $Previous
    }
    if ($Code -ne 0) { throw "$Label failed (exit code $Code)." }
}

function Find-SystemPython {
    $Candidates = @(
        @{ Command = 'py'; Args = @('-3') },
        @{ Command = 'python'; Args = @() }
    )
    foreach ($Candidate in $Candidates) {
        $Launcher = Get-Command $Candidate.Command -ErrorAction SilentlyContinue
        if (-not $Launcher) { continue }
        try {
            $Output = & $Launcher.Source @($Candidate.Args) -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)' 2>$null
            if ($LASTEXITCODE -eq 0 -and ($Output | Out-String).Trim() -eq '1') {
                return $Candidate
            }
        } catch { }
    }
    throw 'Python 3.10 or newer is required. Install it from python.org and rerun run.bat.'
}

function Test-PythonImport([string]$Module) {
    $Previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $PythonExe -c "import $Module" *> $null
        $Code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $Previous
    }
    return $Code -eq 0
}

function Get-ConfiguredPort {
    $ConfigPath = Join-Path $ProjectRoot 'config\config.json'
    if (Test-Path -LiteralPath $ConfigPath) {
        try {
            $ConfigObject = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($ConfigObject.port -and [int]$ConfigObject.port -gt 0 -and [int]$ConfigObject.port -lt 65536) {
                return [int]$ConfigObject.port
            }
        } catch { throw 'Invalid config/config.json. Check the JSON syntax and port.' }
    }
    return 8767
}

function Test-ServiceUp([int]$Port) {
    try {
        $Health = Invoke-RestMethod -Uri ("http://127.0.0.1:$Port/api/health") -TimeoutSec 2
        return $Health.service -eq $ServiceName
    } catch {
        return $false
    }
}

function Stop-OwnListener([int]$Port) {
    # Only stop a process that is provably this project's own server.
    $Health = $null
    try {
        $Health = Invoke-RestMethod -Uri ("http://127.0.0.1:$Port/api/health") -TimeoutSec 2
    } catch { }
    if ($null -eq $Health) { return }
    $RootMatches = [string]::Equals(
        ([string]$Health.root).TrimEnd('\'),
        ([IO.Path]::GetFullPath($ProjectRoot)).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase)
    if ($Health.service -ne $ServiceName -or -not $RootMatches) { return }
    $Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $Listener) { return }
    Stop-Process -Id $Listener.OwningProcess -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
    }
}

try {
    [IO.Directory]::CreateDirectory($RuntimeDir) | Out-Null
    $Port = Get-ConfiguredPort
    $URL = "http://127.0.0.1:$Port/"
    # 每次双击都先停掉本项目旧服务再启动：保证代码改动一定生效。
    Stop-OwnListener $Port
    $Still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($Still) {
        throw "Port $Port is occupied by another service. Close the process using port $Port before rerunning run.bat."
    }

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        $SystemPython = Find-SystemPython
        Write-Host 'Creating local Python environment...'
        $PyExe = (Get-Command $SystemPython.Command).Source
        $PyArgs = @($SystemPython.Args)
        Invoke-Logged { & $PyExe @PyArgs -m venv $VenvDir } 'Python environment creation'
        if (-not (Test-Path -LiteralPath $PythonExe)) {
            throw 'Python environment creation completed without a usable interpreter.'
        }
    }
    $Version = & $PythonExe -c 'import sys; print(sys.version.split()[0])'
    if ($LASTEXITCODE -ne 0) { throw 'Local Python interpreter is not usable.' }
    $VersionOK = & $PythonExe -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)'
    if ($LASTEXITCODE -ne 0 -or ($VersionOK | Out-String).Trim() -ne '1') {
        throw 'Local Python is older than 3.10. Remove runtime/.venv and rerun run.bat.'
    }

    $Fingerprint = ((Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash + '|' + $Version)
    $Saved = if (Test-Path -LiteralPath $DependencyStamp) { (Get-Content $DependencyStamp -Raw).Trim() } else { '' }
    $CoreReady = $Saved -eq $Fingerprint
    if ($CoreReady) {
        $CoreReady = (Test-PythonImport 'fastapi') -and (Test-PythonImport 'uvicorn') -and (Test-PythonImport 'playwright') -and (Test-PythonImport 'PIL') -and (Test-PythonImport 'markdown')
    }
    if (-not $CoreReady) {
        Write-Host 'Installing Python dependencies...'
        Invoke-Logged { & $PythonExe -m ensurepip --upgrade } 'Pip bootstrap'
        Invoke-Logged { & $PythonExe -m pip install --disable-pip-version-check -r $Requirements } 'Dependency installation'
        Invoke-Logged { & $PythonExe -m pip check } 'Dependency verification'
        [IO.File]::WriteAllText($DependencyStamp, $Fingerprint)
        Write-Host 'Dependencies installed.'
    }

    if (Test-Path -LiteralPath $MediaRequirements) {
        $Git = Get-Command git -ErrorAction SilentlyContinue
        $MediaFingerprint = ((Get-FileHash -LiteralPath $MediaRequirements -Algorithm SHA256).Hash + '|' + $Version)
        $MediaSaved = if (Test-Path -LiteralPath $MediaStamp) { (Get-Content $MediaStamp -Raw).Trim() } else { '' }
        $MediaReady = (Test-PythonImport 'mediacloud_uploader') -and ($MediaSaved -eq $MediaFingerprint)
        if (-not $MediaReady) {
            if ($Git) {
                Write-Host 'Installing Zhihu image support...'
                try {
                    Invoke-Logged { & $PythonExe -m pip install --disable-pip-version-check -r $MediaRequirements } 'Zhihu media installation'
                    Invoke-Logged { & $PythonExe -m pip check } 'Dependency verification'
                    if (-not (Test-PythonImport 'mediacloud_uploader')) { throw 'Zhihu uploader import failed.' }
                    [IO.File]::WriteAllText($MediaStamp, $MediaFingerprint)
                    Write-Host 'Zhihu image support installed.'
                } catch {
                    Write-Host 'WARNING: Zhihu image support is unavailable; see runtime/install.log.' -ForegroundColor Yellow
                }
            } else {
                Write-Host 'WARNING: Install Git to enable automatic Zhihu image support.' -ForegroundColor Yellow
            }
        }
    }

    # 独立进程启动服务（src.launch 会在就绪后自动打开浏览器），本窗口只负责验证。
    $ServerProc = Start-Process -FilePath $PythonExe -ArgumentList @('-m', 'src.launch') `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $ServerLog -RedirectStandardError $ServerErrorLog
    $Up = $false
    for ($i = 0; $i -lt 120; $i++) {
        if (Test-ServiceUp $Port) { $Up = $true; break }
        if ($ServerProc.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $Up) {
        throw "Local server failed to start. See runtime/server.log for details."
    }
    Write-Host "Open in browser: $URL"
    exit 0
} catch {
    Show-StartupError $_.Exception.Message
    exit 1
}
