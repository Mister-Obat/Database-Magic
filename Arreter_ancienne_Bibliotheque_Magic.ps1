param(
    [int]$Port = 8765,
    [string]$Python,
    [string]$ProgDir
)

$ErrorActionPreference = 'Stop'
$listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    $listenerPid = [int]$listener.OwningProcess
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid"
    if (-not $process -or $process.CommandLine -notmatch '(?i)(?:^|\s)-m\s+bibliotheque_mtg\.local_app(?:\s|$)') {
        Write-Error "Le port $Port est utilise par un autre programme. Aucun processus n'a ete arrete."
        exit 1
    }
    if ($Python -and $ProgDir -and (Test-Path -LiteralPath $Python -PathType Leaf) -and (Test-Path -LiteralPath $ProgDir -PathType Container)) {
        Push-Location -LiteralPath $ProgDir
        try {
            $revision = & $Python -c 'from bibliotheque_mtg.local_app import APP_API_REVISION; print(APP_API_REVISION)'
            if ($LASTEXITCODE -eq 0) {
                try {
                    $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/v2/health" -TimeoutSec 2
                    if ($health.status -eq 'ok' -and $health.app_api_revision -eq [int]$revision) {
                        Start-Process "http://127.0.0.1:$Port/"
                        exit 0
                    }
                } catch { }
            }
        } finally {
            Pop-Location
        }
    }
    Write-Host "Fermeture de l'ancienne Bibliotheque Magic (PID $listenerPid)..."
    Stop-Process -Id $listenerPid -Force
}

if ($listeners.Count -gt 0) {
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if (-not (Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if (Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        Write-Error "Le port $Port est encore occupe apres l'arret de l'ancienne instance."
        exit 1
    }
}

if (-not $Python -or -not $ProgDir) { exit 0 }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf) -or -not (Test-Path -LiteralPath $ProgDir -PathType Container)) {
    Write-Error 'Le programme Python de la Bibliotheque Magic est introuvable.'
    exit 1
}
$env:PYTHONIOENCODING = 'utf-8'
$env:MAGIC_LIBRARY_PORT = [string]$Port
# A previous launch can still be synchronizing before it opens its socket.
$pending = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath -ieq $Python -and
    $_.CommandLine -match '(?i)(?:^|\s)-m\s+bibliotheque_mtg\.local_app(?:\s|$)'
} | Select-Object -First 1)
$reusedPending = $pending.Count -gt 0
if ($reusedPending) {
    $server = Get-Process -Id $pending[0].ProcessId -ErrorAction Stop
} else {
    $server = Start-Process -FilePath $Python -ArgumentList '-m', 'bibliotheque_mtg.local_app' -WorkingDirectory $ProgDir -WindowStyle Hidden -PassThru
}
$timer = [System.Diagnostics.Stopwatch]::StartNew()
while ($timer.Elapsed.TotalSeconds -lt 300) {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/v2/health" -TimeoutSec 1
        if ($health.status -eq 'ok') {
            if ($reusedPending) { Start-Process "http://127.0.0.1:$Port/" }
            exit 0
        }
    } catch { }
    $server.Refresh()
    if ($server.HasExited) {
        Write-Error "La Bibliotheque Magic s'est arretee avant de demarrer (code $($server.ExitCode))."
        exit 1
    }
    Start-Sleep -Milliseconds 200
}
Write-Error "La Bibliotheque Magic n'a pas repondu apres cinq minutes. Le processus peut encore etre en cours de synchronisation."
exit 1
