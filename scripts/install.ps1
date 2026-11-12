# Luna installer for Windows.
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.ps1 | iex"
# Environment: LUNA_REF, LUNA_REPO, LUNA_NO_MODIFY_PATH=1, LUNA_NO_GIT=1.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = if ($env:LUNA_REPO) { $env:LUNA_REPO } else { 'https://github.com/Dushe4ka/luna-simple' }
$Ref = if ($env:LUNA_REF) { $env:LUNA_REF } else { 'main' }
$ModifyPath = $env:LUNA_NO_MODIFY_PATH -ne '1'
$WantGit = $env:LUNA_NO_GIT -ne '1'

function Say([string]$Text) { Write-Host "luna-install: $Text" }
function Die([string]$Text) { Write-Host "luna-install: ошибка: $Text" -ForegroundColor Red; exit 1 }

$GitPins = @{
  'X64'   = @{ Name = 'PortableGit-2.56.0.4-64-bit.7z.exe'
               Sha256 = 'b1f3e52216e2ed6882d1bebfd4d3fdf319bec40d2edbbf9fd26ec4fd3fce7ed1' }
  'Arm64' = @{ Name = 'PortableGit-2.56.0.4-arm64.7z.exe'
               Sha256 = '044110f9d951858452ddb5e4b5415ea0f2b5b2598e794d6fec77fe28b9aa8cc0' }
}
$GitRelease = 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4'
$LunaLocal = Join-Path $env:LOCALAPPDATA 'luna'
$PortableRoot = Join-Path $LunaLocal 'git'

# bash.exe in %SystemRoot% (System32) and in ...\WindowsApps\ is the WSL launcher, not Git Bash.
function Test-NotWsl([string]$Path) {
  $p = $Path.Replace('/', '\').ToLowerInvariant()
  $root = if ($env:SystemRoot) { $env:SystemRoot } else { 'C:\Windows' }
  $sys = $root.Replace('/', '\').ToLowerInvariant().TrimEnd('\') + '\'
  return -not ($p.StartsWith($sys) -or $p.Contains('\windowsapps\'))
}

function Find-GitBash {
  # Same order as luna_core.shell.find_shell / git_bash_candidates.
  if ($env:LUNA_GIT_BASH_PATH) {
    $e = $env:LUNA_GIT_BASH_PATH
    if ((Test-Path -LiteralPath $e -PathType Leaf) -and (Test-NotWsl $e)) { return $e }
    return $null
  }
  $c = @()
  $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($git) {
    $d = Split-Path -Parent $git.Source
    $c += (Join-Path (Split-Path -Parent $d) 'bin\bash.exe')
    $c += (Join-Path (Split-Path -Parent (Split-Path -Parent $d)) 'bin\bash.exe')
  }
  foreach ($v in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($v) { $c += (Join-Path $v 'Git\bin\bash.exe') }
  }
  $c += (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe')
  $c += (Join-Path $PortableRoot 'bin\bash.exe')
  foreach ($p in $c) { if ((Test-Path -LiteralPath $p -PathType Leaf) -and (Test-NotWsl $p)) { return $p } }
  return $null
}

function Install-PortableGit {
  $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
  $pin = $GitPins[$arch]
  if (-not $pin) { Die "PortableGit для архитектуры $arch нет — поставьте Git вручную: https://git-scm.com/download/win" }
  $tmp = Join-Path ([IO.Path]::GetTempPath()) "luna-git-$PID"
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    $sfx = Join-Path $tmp $pin.Name
    Say "скачиваю $($pin.Name)…"
    Invoke-WebRequest -Uri "$GitRelease/$($pin.Name)" -OutFile $sfx -UseBasicParsing
    $digest = (Get-FileHash -Path $sfx -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -ne $pin.Sha256) { Die "контрольная сумма PortableGit не совпала ($digest)" }
    $staging = "$PortableRoot.staging-$PID"
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    Unblock-File -Path $sfx -ErrorAction SilentlyContinue
    # GUI self-extractor: Start + WaitForExit, `&` would not wait for it.
    $proc = [System.Diagnostics.Process]::Start($sfx, "-o`"$staging`" -y")
    if (-not $proc.WaitForExit(600000)) { taskkill.exe /T /F /PID $proc.Id | Out-Null; Die 'распаковка PortableGit зависла' }
    if ($proc.ExitCode) { Die "распаковка PortableGit завершилась с кодом $($proc.ExitCode)" }
    if (-not (Test-Path (Join-Path $staging 'bin\bash.exe'))) { Die 'в архиве PortableGit нет bin\bash.exe' }
    if (Test-Path -LiteralPath $PortableRoot) {
      attrib.exe -R "$PortableRoot\*" /S /D | Out-Null
      Remove-Item -LiteralPath $PortableRoot -Recurse -Force
    }
    [IO.Directory]::Move($staging, $PortableRoot)
  } finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# 1. uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say 'ставлю uv (менеджер Python от Astral)…'
  if (-not $ModifyPath) { $env:UV_NO_MODIFY_PATH = '1' }
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die 'uv установлен, но не найден в PATH' }
}

# 2. Git Bash: needed by `uv tool install git+…` and by Luna's execute/undo.
if (-not (Find-GitBash)) {
  if ($WantGit) {
    Say 'Git не найден — ставлю PortableGit в папку Luna (системный Git не трогается)'
    Install-PortableGit
  } else {
    Say 'Git не найден, LUNA_NO_GIT=1: команды агента и установка из git работать не будут'
  }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $PortableRoot 'cmd\git.exe'))) {
  $env:Path = "$(Join-Path $PortableRoot 'cmd');$env:Path"
}

# 3. Luna
Say "ставлю Luna ($Ref)…"
uv tool install --python 3.12 --force "luna-simple[all] @ git+$Repo@$Ref"
if ($LASTEXITCODE) { Die 'uv tool install завершился с ошибкой' }

if ($ModifyPath) { uv tool update-shell | Out-Null }

$Bin = (uv tool dir --bin).Trim()
& (Join-Path $Bin 'luna.exe') doctor
if ($LASTEXITCODE) { Say 'doctor нашёл проблемы — подсказки выше' }
Say 'готово. Откройте новый терминал и наберите: luna'
