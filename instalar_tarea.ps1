# Programa el buscador de locales en el Programador de tareas de Windows.
# Todos los dias a las 09:15, 13:15 y 17:15 (cada 4 h por 8 h). Despierta el PC si esta suspendido y enchufado.
#
#   Instalar:     powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1
#   Desinstalar:  powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -Desinstalar
param(
    [int]$CadaHoras = 4,
    [string]$Desde = "09:15",
    [int]$Horas = 8,
    [switch]$Desinstalar
)
$nombre = "Buscador Locales Comerciales"
if ($Desinstalar) {
    Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
    Write-Host "Tarea '$nombre' eliminada."
    return
}
$dir = $PSScriptRoot
$pyw = (& py -c "import sys, pathlib; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))").Trim()
if (-not (Test-Path $pyw)) { $pyw = (Get-Command pyw -ErrorAction Stop).Source }
$accion = New-ScheduledTaskAction -Execute $pyw -Argument "`"$dir\buscador_locales.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At $Desde
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $Desde -RepetitionInterval (New-TimeSpan -Hours $CadaHoras) -RepetitionDuration (New-TimeSpan -Hours $Horas)).Repetition
$opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $trigger -Settings $opciones -Description "Busca locales comerciales en Portal Inmobiliario y avisa los que calzan" -Force | Out-Null
Write-Host "Tarea '$nombre' instalada: todos los dias cada $CadaHoras h desde $Desde por $Horas h."
