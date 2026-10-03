# 编译启动器 -> desktop\dist\rikka.exe（GUI 子系统，无控制台窗口）
# 依赖：MinGW-w64 g++ 与 windres（本机路径 C:\UserProgram\mingw64\bin）
$ErrorActionPreference = 'Stop'

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = Split-Path -Parent $here
$dist   = Join-Path $desktop 'dist'
$icon   = Join-Path $here 'icon.ico'

if (-not (Test-Path $icon)) {
    Copy-Item (Join-Path $desktop 'build\icon.ico') $icon -Force
}
New-Item -ItemType Directory -Force -Path $dist | Out-Null

$obj = Join-Path $here 'launcher.res.o'
$out = Join-Path $dist 'rikka.exe'

Push-Location $here
try {
    # windres 解析 rc 中的 "icon.ico" 时以当前目录为基准
    & windres -i 'launcher.rc' -O coff -o $obj
    if ($LASTEXITCODE -ne 0) { throw "windres failed: $LASTEXITCODE" }

    & g++ -O2 -std=c++17 -mwindows -municode -static -Wall -Wextra `
         -o $out (Join-Path $here 'main.cpp') $obj
    if ($LASTEXITCODE -ne 0) { throw "g++ failed: $LASTEXITCODE" }
}
finally {
    Pop-Location
}

Write-Host "built: $out"
Get-Item $out | Select-Object FullName, Length, LastWriteTime
