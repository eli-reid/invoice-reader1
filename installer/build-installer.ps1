$ErrorActionPreference = 'Stop'

$compilerPath = (Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue).Source
if (-not $compilerPath) {
    $compilerPaths = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    $compilerPath = $compilerPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

if (-not $compilerPath) {
    throw 'Inno Setup 6 is required to build the installer. Install it from https://jrsoftware.org/isinfo.php, then run this script again.'
}

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDirectory = Split-Path -Parent $scriptDirectory
$definitionPath = Join-Path $scriptDirectory 'InvoiceReader.iss'
$buildPython = (Get-Command 'python.exe' -ErrorAction Stop).Source
& $buildPython -m pip --version
if ($LASTEXITCODE -ne 0) {
    throw 'The build computer requires Python with pip to prepare bundled dependencies.'
}

# Qt packages contain deep paths; a short staging name avoids Windows path limits.
$stagingDirectory = Join-Path ([IO.Path]::GetTempPath()) ('IR-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stagingDirectory | Out-Null
try {
    $runtimeDirectory = Join-Path $stagingDirectory 'runtime'
    $archivePath = Join-Path $stagingDirectory 'python.zip'
    $pythonUrl = 'https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip'
    $pythonHash = '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Output 'Downloading the private Python 3.14.10 x64 runtime...'
    Invoke-WebRequest -Uri $pythonUrl -OutFile $archivePath -UseBasicParsing
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pythonHash) {
        throw 'The downloaded Python runtime failed its SHA-256 integrity check.'
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeDirectory

    $packagesDirectory = Join-Path $runtimeDirectory 'Lib\site-packages'
    & $buildPython -m pip install --isolated --disable-pip-version-check --only-binary=:all: --platform win_amd64 --python-version 3.14 --implementation cp --abi cp314 --no-compile --target $packagesDirectory -r (Join-Path $projectDirectory 'requirements-runtime.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Preparing the bundled Python dependencies failed.'
    }

    # Relative paths keep Python isolated and usable after installation in any folder.
    Set-Content -LiteralPath (Join-Path $runtimeDirectory 'python314._pth') -Encoding ASCII -Value @(
        'python314.zip',
        '.',
        'Lib\site-packages',
        '..'
    )
    $runtimePython = Join-Path $runtimeDirectory 'python.exe'
    & $runtimePython -c "import sys; sys.path.insert(0, sys.argv[1]); import invoice_reader.app, invoice_reader.ui.main_window, pdfplumber, pypdfium2; from winrt.windows.media.ocr import OcrEngine; from PySide6.QtWidgets import QApplication; app = QApplication([]); print('Bundled runtime and GUI imports passed:', sys.version)" $projectDirectory
    if ($LASTEXITCODE -ne 0) {
        throw 'The bundled Python runtime failed its application import smoke test.'
    }

    & $compilerPath /Qp "/DRuntimeSource=$runtimeDirectory" $definitionPath
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup compilation failed with exit code $LASTEXITCODE."
    }
    Write-Output "Offline installer created in $(Join-Path $projectDirectory 'dist')."
}
finally {
    Remove-Item -LiteralPath $stagingDirectory -Recurse -Force
}
