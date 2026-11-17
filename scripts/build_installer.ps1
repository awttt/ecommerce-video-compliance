# 在 Windows 上把程序打成一个安装包：dist\VideoCompliance-<版本>-Setup.exe
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
# 重复运行会生成同一份安装包；用户端升级安装会覆盖旧版本，数据目录不受影响。

param(
    [switch]$SkipAppBuild
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Iscc {
    # 先找安装目录，这样能一并拿到 Languages 目录里的语言文件；PATH 里的 choco shim 没有这些。
    $roots = @(
        [Environment]::GetEnvironmentVariable("ProgramFiles(x86)"),
        [Environment]::GetEnvironmentVariable("ProgramFiles")
    )
    foreach ($root in $roots) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        $found = Get-ChildItem -Path $root -Directory -Filter "Inno Setup*" -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "ISCC.exe" } |
            Where-Object { Test-Path $_ } |
            Select-Object -First 1
        if ($found) { return $found }
    }
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Read-AppVersion {
    $init = Join-Path $Root "app\__init__.py"
    $text = Get-Content $init -Raw
    if ($text -notmatch '__version__\s*=\s*"([^"]+)"') {
        throw "没有在 app\__init__.py 里找到 __version__。"
    }
    return $Matches[1]
}

if (-not $SkipAppBuild) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_windows.ps1")
    if ($LASTEXITCODE -ne 0) { throw "构建程序失败，退出码 $LASTEXITCODE。" }
}

$dist = Join-Path $Root "dist\VideoCompliance"
$exe = Join-Path $dist "VideoCompliance.exe"
if (-not (Test-Path $exe)) {
    throw "没有找到 $exe。先运行 scripts\build_windows.ps1，或去掉 -SkipAppBuild。"
}

# 开发文档、原型和测试视频不属于交付物，打包前清掉，避免混进安装包和上传的 dist。
Get-ChildItem -Path $dist -Recurse -File -Include *.md,*.mp4 -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

# 安装包要自带 FFmpeg，否则用户装完打不开任何视频。
if (-not (Test-Path (Join-Path $dist "ffmpeg.exe")) -or -not (Test-Path (Join-Path $dist "ffprobe.exe"))) {
    Write-Host "正在下载 FFmpeg..."
    $zip = Join-Path $env:TEMP "evc-ffmpeg.zip"
    $unpacked = Join-Path $env:TEMP "evc-ffmpeg"
    if (Test-Path $unpacked) { Remove-Item $unpacked -Recurse -Force }
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $unpacked
    $ffmpeg = Get-ChildItem -Path $unpacked -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    if (-not $ffmpeg) { throw "下载的压缩包里没有 ffmpeg.exe。" }
    $ffprobe = Join-Path $ffmpeg.DirectoryName "ffprobe.exe"
    if (-not (Test-Path $ffprobe)) { throw "ffmpeg.exe 旁边没有 ffprobe.exe。" }
    Copy-Item $ffmpeg.FullName, $ffprobe -Destination $dist
}

$iscc = Find-Iscc
if (-not $iscc) {
    throw @"
没有找到 Inno Setup 的编译器 ISCC.exe。
安装方式（任选其一）：
  winget install --id JRSoftware.InnoSetup -e
  choco install innosetup -y
装完重新运行本脚本。下载地址：https://jrsoftware.org/isdl.php
"@
}

# 用编译器自带的中文语言文件，版本一定匹配；没有就退回英文向导。
$compilerLang = Join-Path (Split-Path -Parent $iscc) "Languages\ChineseSimplified.isl"
$isl = Join-Path $Root "packaging\ChineseSimplified.isl"
if (Test-Path $compilerLang) {
    Copy-Item $compilerLang $isl -Force
} elseif (Test-Path $isl) {
    Remove-Item $isl -Force
}

$version = Read-AppVersion
Write-Host "正在生成安装包，版本 $version ..."
& $iscc "/DMyAppVersion=$version" (Join-Path $Root "packaging\installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败，退出码 $LASTEXITCODE。" }

$setup = Join-Path $Root "dist\VideoCompliance-$version-Setup.exe"
if (-not (Test-Path $setup)) { throw "没有生成 $setup。" }

$size = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host ""
Write-Host "安装包：$setup（$size MB）"
Write-Host "用户安装到 %LOCALAPPDATA%\Programs\VideoCompliance，重复安装会覆盖旧版本。"
Write-Host "数据仍在 %APPDATA%\EcommerceVideoCompliance，卸载或升级都不会删除。"
Write-Host "没有代码签名时，Windows 首次运行可能弹 SmartScreen 提示。"
