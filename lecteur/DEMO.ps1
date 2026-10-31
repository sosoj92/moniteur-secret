# Pour filmer la démo en direct : le vrai lecteur, sur le vrai film, ralenti puis de plus en plus rapide.
# Comme TOURNAGE.ps1 : met en pause le fond d'écran animé et réduit les fenêtres (le leurre devient le bureau),
# puis remet tout comme avant à la fin.
# Touches : M lance la démo (et la relance), N passe à l'étape suivante, T masque/affiche les légendes,
#           Haut/Bas règle l'intensité du film, Échap quitte.
# Usage : DEMO.ps1 [video] [-Intensite 0.8]
param([string]$Video = "",   # vide : une fenêtre te demande la vidéo
      [double]$Intensite = 0.8)   # pour la démo, le film doit bien se voir quand l'écran fige l'image A
$we = "${env:ProgramFiles(x86)}\Steam\steamapps\common\wallpaper_engine\wallpaper64.exe"
$weActif = (Test-Path $we) -and (Get-Process wallpaper64 -ErrorAction SilentlyContinue)
if ($weActif) { & $we -control pause; Start-Sleep -Milliseconds 500 }
$shell = New-Object -ComObject Shell.Application
$shell.MinimizeAll()
Start-Sleep -Seconds 1
try {
    & "$PSScriptRoot\LANCER.bat" $Video --intensite $Intensite --disposition plein --demo-touche
} finally {
    $shell.UndoMinimizeALL()
    if ($weActif) { & $we -control play }
}
