$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appDir = Resolve-Path (Join-Path $scriptDir "..\..")
Set-Location $appDir

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 "launchers\start_converter.py"
    $status = $LASTEXITCODE
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python "launchers\start_converter.py"
    $status = $LASTEXITCODE
} else {
    Write-Host "Python 3 не найден."
    Write-Host "Установите Python 3.10 или новее и запустите файл снова."
    Write-Host "Скачать: https://www.python.org/downloads/"
    $status = 1
}

Write-Host ""
Read-Host "Нажмите Enter, чтобы закрыть окно"
exit $status
