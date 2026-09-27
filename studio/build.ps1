# Costruisce MyDevAgent Studio per Windows: VSCodium + l'estensione MyDevAgent + tema e icone di Vio,
# poi l'installer con Inno Setup. Lo lancia .github/workflows/studio.yml dopo aver creato extension\mydevagent.vsix.
# Serve: gh (con GH_TOKEN), node, Inno Setup 6 (se manca lo installa con choco).
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path $PSScriptRoot -Parent
$branding = Join-Path $PSScriptRoot "branding"
$build = Join-Path $root "build"
$app = Join-Path $build "app"
$out = Join-Path $build "out"
$extras = Join-Path $app "extras"
function Controlla($cosa) { if ($LASTEXITCODE) { throw "$cosa non è riuscito (codice $LASTEXITCODE)" } }

Remove-Item $build -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $build, $out | Out-Null
$versione = (Get-Content (Join-Path $root "extension\package.json") -Raw | ConvertFrom-Json).version
$vsix = Join-Path $root "extension\mydevagent.vsix"
if (-not (Test-Path $vsix)) { throw "manca ${vsix}: prima compila l'estensione (npm run package)" }

Write-Host "==> 1/6 VSCodium"
$release = gh release view --repo VSCodium/vscodium --json tagName,assets | ConvertFrom-Json
Controlla "gh release view"
$zip = $release.assets | Where-Object { $_.name -match '^VSCodium-win32-x64-[\d.]+\.zip$' } | Select-Object -First 1
if (-not $zip) { throw "nella release $($release.tagName) di VSCodium non c'è lo zip per Windows x64" }
gh release download $release.tagName --repo VSCodium/vscodium --pattern $zip.name --dir $build
Controlla "gh release download"
Expand-Archive (Join-Path $build $zip.name) $app
Write-Host "    VSCodium $($release.tagName)"

Write-Host "==> 2/6 Nome, cartelle e niente aggiornamenti di VSCodium"
$product = Get-ChildItem $app -Recurse -Filter product.json | Where-Object { $_.FullName -match '[\\/]resources[\\/]app[\\/]product\.json$' } |
  Select-Object -First 1
if (-not $product) { throw "product.json di VSCodium non trovato" }
node (Join-Path $PSScriptRoot "patch-product.js") $product.FullName
Controlla "patch-product.js"
$resources = $product.DirectoryName
$exe = Get-ChildItem $app -Filter *.exe | Where-Object { $_.Name -notmatch '^unins' } | Select-Object -First 1
$cli = Get-ChildItem (Join-Path $app "bin") -Filter *.cmd | Select-Object -First 1
if (-not $exe -or -not $cli) { throw "non trovo l'eseguibile o la CLI di VSCodium in $app" }
Write-Host "    eseguibile: $($exe.Name), CLI: bin\$($cli.Name)"

Write-Host "==> 3/6 Estensione MyDevAgent e impostazioni di Studio"
$extensions = Join-Path $resources "extensions"
$unpacked = Join-Path $build "vsix"
Copy-Item $vsix (Join-Path $build "mydevagent.zip")
Expand-Archive (Join-Path $build "mydevagent.zip") $unpacked
Move-Item (Join-Path $unpacked "extension") (Join-Path $extensions "mydevagent")
Copy-Item (Join-Path $PSScriptRoot "defaults") (Join-Path $extensions "mydevagent-studio-defaults") -Recurse
New-Item -ItemType Directory $extras | Out-Null
Copy-Item (Join-Path $root "extension\setup\installa-mydevagent.ps1") $extras

Write-Host "==> 4/6 Lingua italiana"
$parti = $release.tagName -split "\."
$prefisso = "$($parti[0]).$($parti[1])."
$pack = Invoke-RestMethod "https://open-vsx.org/api/MS-CEINTL/vscode-language-pack-it"
if (-not $pack.version.StartsWith($prefisso) -and $pack.allVersions) {
  $adatte = @($pack.allVersions.PSObject.Properties.Name | Where-Object { $_.StartsWith($prefisso) } |
    Sort-Object { [version]$_ } -Descending)
  if ($adatte) { $pack = Invoke-RestMethod $pack.allVersions.($adatte[0]) }
}
Write-Host "    language pack $($pack.version) per VSCodium $($release.tagName)"
Invoke-WebRequest $pack.files.download -OutFile (Join-Path $extras "italiano.vsix") -UseBasicParsing
# prova: la CLI di Studio deve riuscire a installarla, con lo stesso comando che usa l'installer sul PC
$prova = Join-Path $build "prova"
$dirs = "--extensions-dir `"$prova\ext`" --user-data-dir `"$prova\data`""
foreach ($comando in "--install-extension `"$(Join-Path $extras 'italiano.vsix')`" --force", "--list-extensions --show-versions") {
  $p = Start-Process cmd -ArgumentList "/c `"`"$($cli.FullName)`" $comando $dirs`"" -Wait -NoNewWindow -PassThru
  if ($p.ExitCode) { throw "la CLI di Studio non riesce a installare la lingua italiana (codice $($p.ExitCode))" }
}

Write-Host "==> 5/6 Icone di Vio"
$rcedit = Join-Path $build "rcedit.exe"
Invoke-WebRequest "https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe" -OutFile $rcedit -UseBasicParsing
& $rcedit $exe.FullName --set-icon (Join-Path $branding "vio.ico") `
  --set-version-string FileDescription "MyDevAgent Studio" --set-version-string ProductName "MyDevAgent Studio" `
  --set-version-string CompanyName "MyDevAgent" --set-version-string LegalCopyright "(c) 2026 gio - MyDevAgent Studio. Tutti i diritti riservati. Basato su VSCodium (MIT)"
Controlla "rcedit"
$sostituite = 0
foreach ($file in Get-ChildItem $app -Recurse -File -Include "letterpress-*.svg", "code-icon.svg", "code_150x150.png", "code_70x70.png") {
  $nostro = @{ "code-icon.svg" = "vio.svg"; "code_150x150.png" = "tile-150.png"; "code_70x70.png" = "tile-70.png" }[$file.Name]
  if (-not $nostro) { $nostro = $file.Name }
  $sorgente = Join-Path $branding $nostro
  if (Test-Path $sorgente) { Copy-Item $sorgente $file.FullName -Force; $sostituite++ }
}
Write-Host "    $sostituite immagini di VSCodium sostituite con Vio"

Write-Host "==> 6/6 Installer"
$trova = { Get-ChildItem "${env:ProgramFiles(x86)}\Inno Setup *\ISCC.exe", "$env:ProgramFiles\Inno Setup *\ISCC.exe" -ErrorAction SilentlyContinue |
  Select-Object -First 1 }
$iscc = & $trova
if (-not $iscc) {
  choco install innosetup -y --no-progress | Out-Host
  Controlla "choco install innosetup"
  $iscc = & $trova
}
Write-Host "    $($iscc.FullName)"
& $iscc.FullName "/DVersione=$versione" "/DSorgente=$app" "/DExe=$($exe.Name)" "/DCli=$($cli.Name)" "/DBranding=$branding" "/O$out" `
  (Join-Path $PSScriptRoot "installer.iss")
Controlla "Inno Setup"
Copy-Item $vsix $out
Get-ChildItem $out | ForEach-Object { Write-Host ("    {0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB)) }
