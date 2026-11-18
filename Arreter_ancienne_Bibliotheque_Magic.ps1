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
Start-Process -FilePath $Python -ArgumentList '-m', 'bibliotheque_mtg.local_app' -WorkingDirectory $ProgDir -WindowStyle Hidden | Out-Null
for ($attempt = 0; $attempt -lt 100; $attempt++) {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/v2/health" -TimeoutSec 1
        if ($health.status -eq 'ok') { exit 0 }
    } catch {
        Start-Sleep -Milliseconds 100
    }
}
Write-Error "La Bibliotheque Magic n'a pas repondu apres son lancement."
exit 1
