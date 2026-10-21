param([string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$previousPyInstallerConfig = $env:PYINSTALLER_CONFIG_DIR
$previousPath = $env:PATH
Push-Location -LiteralPath $projectRoot
try {
    $version = (Get-Content -LiteralPath 'version.txt' -Raw).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?$') { throw 'Invalid version.txt' }
    if (-not $OutputDirectory) { $OutputDirectory = Join-Path 'dist' $version }
    $binaryName = "wart-dapp-$version-windows-x64.exe"
    $executable = Join-Path $OutputDirectory $binaryName
    New-Item -ItemType Directory -Path 'build\tmp' -Force | Out-Null
    $env:TEMP = Join-Path $projectRoot 'build\tmp'
    $env:TMP = $env:TEMP
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot 'build\pyinstaller-cache'
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install --index-url https://pypi.org/simple -r requirements-windows.lock
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed.' }
    & '.\.venv\Scripts\python.exe' scripts\write-build-info.py
    if ($LASTEXITCODE -ne 0) { throw 'Build metadata generation failed.' }
    # Do not bundle unrelated DLLs found in developer tools on the ambient PATH.
    # Qt uses Windows' ICU; another application's icuuc.dll may be incompatible.
    $basePython = & '.\.venv\Scripts\python.exe' -c 'import sys; print(sys.base_prefix)'
    $env:PATH = @((Join-Path $projectRoot '.venv\Scripts'), $basePython,
        (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm --clean --distpath $OutputDirectory --workpath build\pyinstaller main.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & '.\.venv\Scripts\python.exe' scripts\smoke-binary.py $executable
    if ($LASTEXITCODE -ne 0) { throw 'Packaged executable startup failed.' }
    Copy-Item -LiteralPath 'scripts\Start-Test-Wallet.cmd' -Destination (Join-Path $OutputDirectory 'Start-Test-Wallet.cmd')
    Copy-Item -LiteralPath 'scripts\Start-Wallet.cmd' -Destination (Join-Path $OutputDirectory 'Start-Wallet.cmd')
    Copy-Item -LiteralPath 'README.md' -Destination (Join-Path $OutputDirectory 'README.md')
    Copy-Item -LiteralPath 'LICENSE' -Destination (Join-Path $OutputDirectory 'LICENSE')
    Copy-Item -LiteralPath 'version.txt' -Destination (Join-Path $OutputDirectory 'version.txt')
    Copy-Item -LiteralPath 'build\generated\build-info.json' -Destination (Join-Path $OutputDirectory 'build-info.json')
    $packageFiles = @($binaryName, 'Start-Wallet.cmd', 'Start-Test-Wallet.cmd', 'README.md', 'LICENSE', 'version.txt', 'build-info.json')
    $checksums = foreach ($name in $packageFiles) {
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutputDirectory $name)).Hash.ToLowerInvariant()
        "$digest  $name"
    }
    $checksums | Set-Content -Encoding ascii -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS.txt')
    $archive = Join-Path $OutputDirectory "wart-dapp-$version-windows-x64.zip"
    $sources = @($packageFiles + 'SHA256SUMS.txt' | ForEach-Object { Join-Path $OutputDirectory $_ })
    Compress-Archive -LiteralPath $sources -DestinationPath $archive -Force
    $archiveDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
    "$archiveDigest  $([IO.Path]::GetFileName($archive))" | Add-Content -Encoding ascii -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS.txt')
    & '.\.venv\Scripts\python.exe' scripts\check-package.py $OutputDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Release package verification failed.' }
    Get-FileHash -Algorithm SHA256 -LiteralPath $executable
} finally {
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    $env:PYINSTALLER_CONFIG_DIR = $previousPyInstallerConfig
    $env:PATH = $previousPath
    Pop-Location
}
