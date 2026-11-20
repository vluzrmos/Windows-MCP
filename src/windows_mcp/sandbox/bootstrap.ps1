# Starts windows-mcp inside a separate Windows session (Windows Sandbox, Hyper-V VM, RDS user).
#
# Run it as the logon command of that session. It installs uv when missing, prepares
# windows-mcp (from a mapped source checkout when one exists, otherwise from PyPI), starts
# the server on streamable HTTP with a bearer key, and writes ready.json to the shared folder
# so the host knows where to connect.
#
# Inputs (all in -Shared): auth.key (required, created by the host). Logs go to
# <Shared>\logs. Cache (uv, Python, wheels) goes to -Cache so later boots are fast.

param(
    [string]$Shared = "C:\mcp\shared",
    [string]$Source = "C:\mcp\source",
    [string]$Cache = "C:\mcp\cache",
    [string]$Work = "C:\mcp-work",
    [int]$Port = 8765,
    [string]$Allowlist = ""
)

$ErrorActionPreference = "Stop"
$logDir = Join-Path $Shared "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir "bootstrap.log") -Append | Out-Null

function Write-Ready([hashtable]$Data) {
    $Data.time = (Get-Date).ToString("o")
    $tmp = Join-Path $Shared "ready.json.tmp"
    $Data | ConvertTo-Json | Set-Content -Path $tmp -Encoding utf8
    Move-Item -Force $tmp (Join-Path $Shared "ready.json")
}

try {
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $Shared "ready.json")

    $keyFile = Join-Path $Shared "auth.key"
    if (-not (Test-Path $keyFile)) { throw "Missing $keyFile (the host writes it before start)." }
    $authKey = (Get-Content -Raw $keyFile).Trim()

    # uv and its downloads live in the cache folder, which survives a disposable sandbox.
    New-Item -ItemType Directory -Force -Path $Cache | Out-Null
    $env:UV_CACHE_DIR = Join-Path $Cache "uv-cache"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Cache "python"
    $env:UV_LINK_MODE = "copy"   # hard links cannot cross into a mapped folder
    $uvDir = Join-Path $Cache "uv-bin"
    $uv = Join-Path $uvDir "uv.exe"
    if (-not (Test-Path $uv)) {
        Write-Host "Installing uv into $uvDir"
        $env:UV_INSTALL_DIR = $uvDir
        $env:UV_NO_MODIFY_PATH = "1"
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    }

    # The person never watches this session: skip the consent countdown, and let only the
    # keyboard (the takeover hotkey) stop the AI so a bumped mouse does not.
    $configPath = Join-Path $Work "config.toml"
    New-Item -ItemType Directory -Force -Path $Work | Out-Null
    "[control]`nconsent_countdown_seconds = 0`nmouse_takeover = false`n" |
        Set-Content -Path $configPath -Encoding utf8
    # A fresh sandbox has no OCR language packs, so screenshots go back as images. (The vision
    # settings are read from the environment or ~/.windows-mcp/config.toml, not --config.)
    if (-not $env:WINDOWS_MCP_VISION_MODE) { $env:WINDOWS_MCP_VISION_MODE = "image" }

    if (Test-Path (Join-Path $Source "pyproject.toml")) {
        # Copy the checkout so .venv is written locally, not into the read-only mapping.
        $app = Join-Path $Work "app"
        robocopy $Source $app /MIR /XD .venv .git __pycache__ .pytest_cache .ruff_cache /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
        # Log uv's output (the transcript misses native output) and retry once: the first
        # boot downloads Python and every wheel, and a dropped download fails the sync.
        $syncLog = Join-Path $logDir "uv-sync.log"
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            cmd /c "`"$uv`" sync --frozen --project `"$app`" >> `"$syncLog`" 2>&1"
            if ($LASTEXITCODE -eq 0) { break }
            Write-Host "uv sync attempt $attempt failed with exit code $LASTEXITCODE (see $syncLog)"
            if ($attempt -eq 2) { throw "uv sync failed with exit code $LASTEXITCODE; see $syncLog" }
        }
        $exe = $uv
        $prefix = @("run", "--frozen", "--project", $app, "windows-mcp")
    }
    else {
        $exe = $uv
        $prefix = @("tool", "run", "windows-mcp")
    }

    # Clients reach the server from the host side of the virtual switch: allow that subnet.
    $ipInfo = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        Select-Object -First 1
    if (-not $ipInfo) { throw "The session has no IPv4 address (is networking enabled?)." }
    if (-not $Allowlist) {
        $net = [System.Net.IPAddress]::Parse($ipInfo.IPAddress).GetAddressBytes()
        $bits = $ipInfo.PrefixLength
        for ($i = 0; $i -lt 4; $i++) {
            $keep = [Math]::Max(0, [Math]::Min(8, $bits - 8 * $i))
            $net[$i] = $net[$i] -band ((0xFF -shl (8 - $keep)) -band 0xFF)
        }
        $Allowlist = "{0}/{1}" -f ([System.Net.IPAddress]::new($net)), $bits
    }

    $rule = "windows-mcp $Port"
    if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port `
            -RemoteAddress $Allowlist -Action Allow | Out-Null
    }

    $env:WINDOWS_MCP_AUTH_KEY = $authKey
    $serverArgs = $prefix + @(
        "serve", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "$Port",
        "--ip-allowlist", $Allowlist, "--config", $configPath
    )
    Write-Host "Starting: $exe $($serverArgs -join ' ')"
    Start-Process -FilePath $exe -ArgumentList $serverArgs -WindowStyle Minimized `
        -RedirectStandardOutput (Join-Path $logDir "server.out.log") `
        -RedirectStandardError (Join-Path $logDir "server.err.log")

    $deadline = (Get-Date).AddMinutes(5)
    while (-not (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue)) {
        if ((Get-Date) -gt $deadline) { throw "Server did not open port $Port within 5 minutes." }
        Start-Sleep -Seconds 2
    }

    Write-Ready @{ status = "ready"; ip = $ipInfo.IPAddress; port = $Port; allowlist = $Allowlist }
    Write-Host "windows-mcp ready on $($ipInfo.IPAddress):$Port"
}
catch {
    Write-Host "Bootstrap failed: $_"
    Write-Ready @{ status = "error"; error = "$_" }
}
finally {
    Stop-Transcript | Out-Null
}
