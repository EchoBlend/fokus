# Baut Fokus.exe und das Setup. Version kommt aus VERSION in fokus.py.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$version = (Select-String -Path "$root\fokus.py" -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value

& "$root\.venv\Scripts\python.exe" "$root\test_fokus.py"
if ($LASTEXITCODE) { throw "Tests fehlgeschlagen" }

& "$root\.venv\Scripts\pyinstaller.exe" --noconfirm --onefile --windowed --name Fokus `
    --icon "$root\assets\fokus.ico" --hidden-import pystray._win32 `
    --distpath "$root\dist" --workpath "$root\build" --specpath "$root\build" "$root\fokus.py"
if ($LASTEXITCODE) { throw "PyInstaller fehlgeschlagen" }

& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" /Q "/DMyAppVersion=$version" "$root\installer\Fokus.iss"
if ($LASTEXITCODE) { throw "Inno Setup fehlgeschlagen" }
Write-Host "Fertig: dist\installer\Fokus-Setup-$version.exe"
