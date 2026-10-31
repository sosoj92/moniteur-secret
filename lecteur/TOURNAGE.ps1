# Pour filmer : met en pause le fond d'écran animé (Wallpaper Engine occupe la carte graphique et fait
# sauter des images, donc voir le secret), réduit toutes les fenêtres (le leurre devient le bureau),
# puis lance le lecteur en mode lunettes. Tout est remis comme avant à la fin. Usage : TOURNAGE.ps1 [video]
param([string]$Video = "",   # vide : une fenêtre te demande la vidéo
      [double]$Intensite = 0.25)   # à 0.6, l'œil nu devine le film ; Haut/Bas pour ajuster en direct
$we = "${env:ProgramFiles(x86)}\Steam\steamapps\common\wallpaper_engine\wallpaper64.exe"
$weActif = (Test-Path $we) -and (Get-Process wallpaper64 -ErrorAction SilentlyContinue)
if ($weActif) { & $we -control pause; Start-Sleep -Milliseconds 500 }
$shell = New-Object -ComObject Shell.Application
$shell.MinimizeAll()
Start-Sleep -Seconds 1
try {
    & "$PSScriptRoot\LANCER-LUNETTES.bat" $Video --intensite $Intensite
} finally {
    $shell.UndoMinimizeALL()
    if ($weActif) { & $we -control play }
}
