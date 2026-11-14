# Luna installer for Windows.
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.ps1 | iex"
# Environment: LUNA_REF, LUNA_REPO, LUNA_NO_MODIFY_PATH=1, LUNA_NO_GIT=1.
function Install-Luna {
  $ErrorActionPreference = 'Stop'
  $ProgressPreference = 'SilentlyContinue'

  $Repo = if ($env:LUNA_REPO) { $env:LUNA_REPO } else { 'https://github.com/Dushe4ka/luna-simple' }
  $Ref = if ($env:LUNA_REF) { $env:LUNA_REF } else { 'main' }
  $ModifyPath = $env:LUNA_NO_MODIFY_PATH -ne '1'
  $WantGit = $env:LUNA_NO_GIT -ne '1'

  function Say([string]$Text) { Write-Host "luna-install: $Text" }
  function Die([string]$Text) { throw "luna-install: error: $Text" }

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
    if (-not $pin) { Die "no PortableGit for architecture $arch - install Git manually: https://git-scm.com/download/win" }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "luna-git-$PID"
    $staging = "$PortableRoot.staging-$PID"
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
      $sfx = Join-Path $tmp $pin.Name
      Say "downloading $($pin.Name)..."
      Invoke-WebRequest -Uri "$GitRelease/$($pin.Name)" -OutFile $sfx -UseBasicParsing
      $digest = (Get-FileHash -Path $sfx -Algorithm SHA256).Hash.ToLowerInvariant()
      if ($digest -ne $pin.Sha256) { Die "PortableGit checksum mismatch ($digest)" }
      New-Item -ItemType Directory -Force -Path $staging | Out-Null
      Unblock-File -Path $sfx -ErrorAction SilentlyContinue
      # GUI self-extractor: Start + WaitForExit, `&` would not wait for it.
      $proc = [System.Diagnostics.Process]::Start($sfx, "-o`"$staging`" -y")
      if (-not $proc.WaitForExit(600000)) { taskkill.exe /T /F /PID $proc.Id | Out-Null; Die 'PortableGit extraction hung' }
      if ($proc.ExitCode) { Die "PortableGit extraction failed with code $($proc.ExitCode)" }
      if (-not (Test-Path (Join-Path $staging 'bin\bash.exe'))) { Die 'the PortableGit archive has no bin\bash.exe' }
      if (Test-Path -LiteralPath $PortableRoot) {
        attrib.exe -R "$PortableRoot\*" /S /D | Out-Null
        Remove-Item -LiteralPath $PortableRoot -Recurse -Force
      }
      [IO.Directory]::Move($staging, $PortableRoot)
    } finally {
      Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
      if (Test-Path -LiteralPath $staging) {
        attrib.exe -R "$staging\*" /S /D | Out-Null
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
      }
    }
  }

  # 1. uv
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say 'installing uv (the Python manager from Astral)...'
    $hadNoModify = Test-Path Env:UV_NO_MODIFY_PATH
    $oldNoModify = $env:UV_NO_MODIFY_PATH
    try {
      if (-not $ModifyPath) { $env:UV_NO_MODIFY_PATH = '1' }
      Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } finally {
      if ($hadNoModify) { $env:UV_NO_MODIFY_PATH = $oldNoModify } else { Remove-Item Env:UV_NO_MODIFY_PATH -ErrorAction SilentlyContinue }
    }
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die 'uv was installed but is not on PATH' }
  }

  # 2. Git Bash and git: `uv tool install git+...` needs git, Luna's execute/undo need Git Bash.
  function Test-Git { [bool](Get-Command git -CommandType Application -ErrorAction SilentlyContinue) }
  $bash = Find-GitBash
  if ($bash -and -not (Test-Git)) {
    # Git Bash found but git is not on PATH: adopt the cmd dir of the same Git install.
    $root = Split-Path -Parent (Split-Path -Parent $bash)
    $cmd = Join-Path $root 'cmd'
    if (Test-Path -LiteralPath (Join-Path $cmd 'git.exe')) { $env:Path = "$cmd;$env:Path" }
  }
  if (-not $bash -or -not (Test-Git)) {
    if ($WantGit) {
      Say 'Git not found - installing PortableGit into the Luna folder (system Git is not touched)'
      Install-PortableGit
      $env:Path = "$(Join-Path $PortableRoot 'cmd');$env:Path"
    } elseif (-not (Test-Git)) {
      Die 'git is required: uv installs Luna from GitHub. Install Git for Windows (https://git-scm.com/download/win) or unset LUNA_NO_GIT=1'
    } else {
      Say 'Git Bash not found and LUNA_NO_GIT=1: agent commands will not work'
    }
  }

  # 3. Luna
  Say "installing Luna ($Ref)..."
  uv tool install --python 3.12 --force "luna-simple[all] @ git+$Repo@$Ref"
  if ($LASTEXITCODE) { Die 'uv tool install failed' }

  if ($ModifyPath) { uv tool update-shell | Out-Null }

  $Bin = (uv tool dir --bin).Trim()
  $env:Path = "$Bin;$env:Path"
  & (Join-Path $Bin 'luna.exe') doctor
  if ($LASTEXITCODE) { Say 'doctor found problems - see the hints above' }
  Say 'done. Open a new terminal and type: luna'
}

try {
  Install-Luna
} catch {
  Write-Host $_.Exception.Message -ForegroundColor Red
  # Under `irm | iex` just return (do not close the user's window); from a file set the exit code.
  if ($MyInvocation.MyCommand.Path) { exit 1 }
}
