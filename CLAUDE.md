# Fokus-App – Projektregeln

## Release nach jeder Änderung (Timo 31.10.)
Nach jeder fertigen Änderung ohne Rückfrage komplett ausliefern:
1. `VERSION` in `fokus.py` + `version` in `chrome-ext/manifest.json` hochzählen (gleich halten)
2. Build: `powershell -File build.ps1` (Tests -> PyInstaller -> Inno-Setup `dist\installer\Fokus-Setup-<v>.exe`)
3. Commit (Conventional Commits, Version in Klammern) + `git push`
4. Install: Setup mit `/VERYSILENT` ausführen, danach `Fokus.exe` neu starten
5. Release: `gh release create v<v> -R EchoBlend/fokus` mit Setup-Exe + `chrome-ext.zip`

Ausnahme: Arbeiten andere Agenten/Sessions parallel in `V:\Workflow`, dann nicht ausliefern – das macht der Letzte, der fertig wird.
