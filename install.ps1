# Installs or updates Roostfile (栖档) for the current Windows user from the latest GitHub release.
#
#   irm https://raw.githubusercontent.com/weichiwes/roostfile/main/install.ps1 | iex
#
# Puts the portable build in %LOCALAPPDATA%\Roostfile (no admin rights needed), keeps an existing library when
# updating, adds Start menu and desktop shortcuts, and opens the app. It reads none of your files: Roostfile itself
# asks which folders to read the first time it opens. Nothing else on the computer is changed.
# Everything runs in its own scope: irm | iex executes in the caller's session, and its variables and preferences
# would otherwise stay behind in the person's PowerShell window.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is many times slower while it draws a progress bar
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $repo = 'weichiwes/roostfile'
    $assetName = 'Roostfile-windows-x64.zip'
    $target = Join-Path $env:LOCALAPPDATA 'Roostfile'
    $work = Join-Path ([IO.Path]::GetTempPath()) ('roostfile-install-' + [guid]::NewGuid().ToString('N'))
    # Chinese if the display language, the regional format or the language for non-Unicode programs is Chinese:
    # many people in China run English Windows with a Chinese region.
    $zh = [bool](@((Get-UICulture).Name, (Get-Culture).Name, $(try { (Get-WinSystemLocale).Name } catch { '' })) -like 'zh*')
    function Say($chinese, $english) { Write-Host ('[Roostfile] ' + $(if ($zh) { $chinese } else { $english })) }

    try {
        if (-not [Environment]::Is64BitOperatingSystem) {
            throw $(if ($zh) { '栖档只支持 64 位 Windows。' } else { 'Roostfile needs 64-bit Windows.' })
        }
        # Updating while it runs would replace files in use. It keeps running in the tray after its window closes.
        $running = Get-Process -Name pythonw, python -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($target, [StringComparison]::OrdinalIgnoreCase) }
        if ($running) {
            throw $(if ($zh) { '栖档正在运行。请在任务栏右下角的托盘图标上右键选择退出，再运行一次这条命令。' }
                    else { 'Roostfile is running. Right-click its tray icon, choose Quit, then run this command again.' })
        }

        Say '正在查找最新版本……' 'Looking up the latest release...'
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'roostfile-installer' }
        $asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
        if (-not $asset) { throw "No $assetName in release $($release.tag_name)." }

        New-Item -ItemType Directory -Path $work | Out-Null
        $zip = Join-Path $work $assetName
        Say "正在下载 $($release.tag_name)（约 $([math]::Round($asset.size / 1MB)) MB）……" "Downloading $($release.tag_name) (about $([math]::Round($asset.size / 1MB)) MB)..."
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing -Headers @{ 'User-Agent' = 'roostfile-installer' }

        # GitHub publishes a SHA-256 for every release file; a download that does not match is not installed.
        if ($asset.digest -and $asset.digest.StartsWith('sha256:')) {
            $expected = $asset.digest.Substring(7)
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash
            if ($actual -ne $expected) { throw "Checksum mismatch: expected $expected, got $actual." }
            Say '校验通过。' 'Checksum verified.'
        }

        Say '正在解压……' 'Unpacking...'
        $unpacked = Join-Path $work 'unpacked'
        Expand-Archive -LiteralPath $zip -DestinationPath $unpacked
        $source = Get-ChildItem -LiteralPath $unpacked -Recurse -Filter 'start.cmd' | Select-Object -First 1
        if (-not $source) { throw 'start.cmd is missing from the download.' }
        $source = $source.DirectoryName

        # Updating: the library (data\ and data-location.txt) stays; everything else is replaced.
        $keep = @('data', 'data-location.txt')
        $fresh = -not (Test-Path -LiteralPath $target)
        if ($fresh) { New-Item -ItemType Directory -Path $target | Out-Null }
        else { Get-ChildItem -LiteralPath $target -Force | Where-Object { $keep -notcontains $_.Name } | Remove-Item -Recurse -Force }
        Get-ChildItem -LiteralPath $source -Force | Where-Object { $keep -notcontains $_.Name } |
            Copy-Item -Destination $target -Recurse -Force
        Get-ChildItem -LiteralPath $target -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

        # A first install on Chinese-language Windows (see $zh) opens in Chinese. Settings can switch it; an existing library keeps its choice.
        $state = Join-Path $target 'data\ui-state.json'
        if ($zh -and -not (Test-Path -LiteralPath (Join-Path $target 'data-location.txt')) -and -not (Test-Path -LiteralPath $state)) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $state) -Force | Out-Null
            [IO.File]::WriteAllText($state, '{"language": "zh"}', (New-Object Text.UTF8Encoding $false))
        }

        $shell = New-Object -ComObject WScript.Shell
        $name = $(if ($zh) { '栖档 Roostfile.lnk' } else { 'Roostfile.lnk' })
        foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
            if (-not $folder -or -not (Test-Path -LiteralPath $folder)) { continue }
            # WScript.Shell saves through the ANSI code page, which turns 栖档 into ?? on English Windows, so the
            # shortcut is saved under a plain name and given its real name by a rename. A shortcut that can't be
            # made is reported; the app is installed either way.
            try {
                $temp = Join-Path $folder 'Roostfile-new.lnk'
                $link = $shell.CreateShortcut($temp)
                $link.TargetPath = Join-Path $target 'start.cmd'
                $link.WorkingDirectory = $target
                $link.WindowStyle = 7  # minimized: the launcher's console closes as soon as the window opens
                $link.Save()
                Move-Item -LiteralPath $temp -Destination (Join-Path $folder $name) -Force
            }
            catch {
                Say "没能在 $folder 里创建快捷方式：$($_.Exception.Message)" "Could not make a shortcut in ${folder}: $($_.Exception.Message)"
            }
        }

        Say "已装好：$target" "Installed in $target"
        Say '桌面和开始菜单里有“栖档 Roostfile”。第一次打开时，选好你平时放文件的文件夹，栖档读过之后就能搜了。' `
            'Shortcuts are on the desktop and in the Start menu. The first time it opens, pick the folders you keep files in; once Roostfile has read them, you can search.'
        Start-Process -FilePath (Join-Path $target 'start.cmd') -WorkingDirectory $target -WindowStyle Hidden
    }
    catch {
        Write-Host ('[Roostfile] ' + $_.Exception.Message) -ForegroundColor Red
    }
    finally {
        if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
    }
}
