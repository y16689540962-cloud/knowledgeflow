<#
.SYNOPSIS
    KnowledgeFlow · Windows x64 便携版构建脚本

.DESCRIPTION
    clean → 取内嵌 Python runtime → 装依赖 → 组装目录 → 构建 exe → 冒烟测试 → 打 ZIP

    产物：dist\KnowledgeFlow-<version>-windows-x64.zip

    用法：
        .\scripts\build_windows_x64.ps1                 # 完整构建（含 ASR/OCR）
        .\scripts\build_windows_x64.ps1 -Minimal        # 不带 ASR/OCR，包更小
        .\scripts\build_windows_x64.ps1 -NoZip          # 只出目录，不打 ZIP
        .\scripts\build_windows_x64.ps1 -Clean          # 先清掉 build\ 再构建

    设计取舍（与 scripts/build_macos_arm64.sh 对齐，说明写在代码里，免得下次有人"优化"掉）：

    1. **不重写后端**。ZIP 里的 backend\ 就是仓库里那份代码，原样拷进去。
       打包层只做「内嵌解释器 + 环境变量映射 + 启动」，业务一行不动。
    2. **不内嵌 ffmpeg**。本项目**不调用 ffmpeg CLI** —— 音视频解码走 PyAV
       （它的 wheel 自带 ffmpeg 库）。所以「系统没装 ffmpeg」不该拦人。
    3. **可选能力单独装**：ASR/OCR 的依赖装失败只降级、不中断构建。
    4. **不做代码签名**。没有代码签名证书就不要假装有：首次运行可能被
       SmartScreen 拦一下，README 里写清怎么放行，**不要求用户关闭系统安全功能**。
    5. **入口用 PyInstaller 冻一个 exe**，而不是让用户双击 .cmd。
       理由：.cmd 会留下一个黑色控制台窗口，而关掉那个窗口会连带杀掉后端 ——
       用户会以为「程序自己退了」。代价是包里多一个约 12MB 的 exe
       （它自带一份解释器，只用来跑启动器；后端仍然用 runtime\ 里那份）。
       之所以不改成「用 runtime 跑一个 .vbs」：.vbs 会被安全软件重点关照，
       观感也不像正规软件。
    6. **删目录一律走 `Remove-Tree`（.NET API），不用 `Remove-Item`**。
       PowerShell 5.1 的 Remove-Item 在长路径（>260 字符）和只读文件上会
       **删到一半停下**，而这里要删的 `build\runtime\` 与 `build\pywork\`
       恰恰是最深的那些。详见函数注释。
    7. **冒烟测试要模拟用户的机器，不是构建机**。第 7 步会临时清掉
       `PYTHONIOENCODING` / `PYTHONUTF8`（并在 finally 里恢复）—— 构建机上
       通常设了 `utf-8`，那会让内嵌解释器绕过 GBK 输出问题，于是
       「中文 Windows 上 `--check` 抛 UnicodeEncodeError」这个真实缺陷
       在构建机上永远测不出来。**别把这两行当噪音删掉。**

.NOTES
    需要：Windows 10+ x64、PowerShell 5.1+、能访问 GitHub 与 PyPI。
    国内网络下 files.pythonhosted.org 常被墙（表现为 pip 报「找不到包」，
    其实是下不动），用镜像即可：

        $env:KF_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
        .\scripts\build_windows_x64.ps1

    **这个文件必须以 UTF-8 *带 BOM* 保存。** Windows PowerShell 5.1 读 .ps1 时，
    没有 BOM 就按系统 ANSI 码页解（中文机器上是 GBK）—— 本脚本里全是中文注释，
    解错之后某些多字节序列会把后面的引号「吃掉」，于是 PowerShell 报的是一堆
    莫名其妙的语法错误（实测：「表达式或语句中包含意外的标记"stage'」、
    「此语言版本中不支持"from"关键字」），而真正的原因（编码）一个字都不提。
    用 VS Code / 记事本另存时请选「UTF-8 with BOM」。
    测试里有一条 `test_build_script_is_utf8_with_bom` 守着它。
#>

[CmdletBinding()]
param(
    [switch]$Minimal,
    [switch]$NoZip,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PkgDir = Join-Path $RepoRoot 'packaging\windows'
$BuildDir = Join-Path $RepoRoot 'build'
$DistDir = Join-Path $RepoRoot 'dist'
$AppName = 'KnowledgeFlow'

$PyProject = Join-Path $RepoRoot 'backend\pyproject.toml'
$Version = '0.4.0'
foreach ($line in Get-Content $PyProject) {
    if ($line -match '^\s*version\s*=\s*"([^"]+)"') {
        $Version = $Matches[1]
        break
    }
}

# python-build-standalone：可重定位的独立 Python，适合整个塞进包里。
# 与 macOS 版**同一个 tag、同一个 Python 版本** —— 两个平台的运行时版本对齐，
# 免得「同一个项目在 Mac 上 3.13.15、在 Windows 上 3.13.9」这种没必要的差异。
$PbsTag = '20260929'
$PbsPython = '3.13.15'
$PbsAsset = "cpython-$PbsPython+$PbsTag-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
$PbsUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$PbsTag/$PbsAsset"
# 下载缓存放在仓库外：这样 -Clean 不会把 30MB 的 runtime 反复重下
$CacheDir = Join-Path $env:LOCALAPPDATA "$AppName-build"

$WithMedia = -not $Minimal
$MediaStatus = '未包含（构建中断）'
$ZipPath = Join-Path $DistDir "$AppName-$Version-windows-x64.zip"
$StageName = "$AppName-$Version-windows-x64"
$StageRoot = Join-Path $BuildDir 'stage'
$Stage = Join-Path $StageRoot $StageName

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "▸ $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "  $Text" -ForegroundColor Green
}

function Write-Warn([string]$Text) {
    Write-Host "  [警告] $Text" -ForegroundColor Yellow
}

function Die([string]$Text) {
    Write-Host ''
    Write-Host "[失败] $Text" -ForegroundColor Red
    exit 1
}

function Invoke-Native {
    # 跑一个原生程序（curl / tar / robocopy / pip / python），返回它的退出码。
    #
    # **为什么必须包一层**：PowerShell 5.1 在 `$ErrorActionPreference = 'Stop'` 下，
    # 只要原生程序往 **stderr** 写了任何一行，就会抛 `NativeCommandError` 当场终止
    # 脚本 —— 哪怕那个程序最后退出码是 0。而这类程序往 stderr 写东西是家常便饭：
    # curl 的下载进度条、pip 的弃用警告、robocopy 的统计。实测就是 curl 的进度条
    # 把「2/8 准备内嵌 Python runtime」这一步打断的。
    #
    # 所以这里临时把 EAP 放回 Continue，**用退出码判断成败**（调用方显式检查）。
    # `2>&1` 把 stderr 并进正常输出，免得它被 PowerShell 包装成错误记录。
    param(
        [string]$Program,
        [string[]]$Arguments,
        [switch]$Capture,   # 捕获输出到 $script:NativeOutput
        [switch]$Quiet      # 丢弃输出
    )
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Capture) {
            $script:NativeOutput = @(& $Program @Arguments 2>&1 | ForEach-Object { "$_" })
        } elseif ($Quiet) {
            & $Program @Arguments 2>&1 | Out-Null
        } else {
            & $Program @Arguments 2>&1 | Out-Host
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
}

function Invoke-Pip {
    param([string]$Python, [string[]]$Arguments)
    $flags = @(
        '--disable-pip-version-check', '--no-warn-script-location',
        '--timeout', '60', '--retries', '8'
    )
    if ($env:KF_PIP_INDEX_URL) {
        $flags += @('--index-url', $env:KF_PIP_INDEX_URL)
    }
    # 输出必须走 Out-Host（Invoke-Native 里做了）。否则 pip 的 stdout 会被
    # `$code = Invoke-Pip ...` 一起捕获，$code 变成 @(几十行输出..., 退出码)；
    # 而 PowerShell 里「数组 -ne 0」是**过滤**语义（返回所有不等于 0 的元素），
    # 非空数组恒为真 —— 于是每一次安装都会被判成失败。
    return Invoke-Native -Program $Python -Arguments (@('-m', 'pip', 'install') + $flags + $Arguments)
}

function Clear-ReadOnly {
    # 递归摘掉只读属性。枚举中途出错不致命 —— 调用方紧接着的 Delete
    # 会把真实原因报出来，比在这里吞掉一个枚举异常有用。
    param([Parameter(Mandatory)][string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith('\\?\')) { $full = '\\?\' + $full }

    try {
        $targets = @($full) + @(
            [System.IO.Directory]::EnumerateFileSystemEntries(
                $full, '*', [System.IO.SearchOption]::AllDirectories
            )
        )
    } catch {
        $targets = @($full)
    }
    foreach ($item in $targets) {
        try {
            $attrs = [System.IO.File]::GetAttributes($item)
            if ($attrs -band [System.IO.FileAttributes]::ReadOnly) {
                [System.IO.File]::SetAttributes(
                    $item, ($attrs -bxor [System.IO.FileAttributes]::ReadOnly)
                )
            }
        } catch {
            Write-Verbose "摘只读属性失败（跳过）：$item"
        }
    }
}

function Remove-Tree {
    # 递归删除一个文件或目录。**故意不用 Remove-Item**，两个真实原因：
    #
    # 1. **长路径**。Windows PowerShell 5.1 的 Remove-Item 走的是旧的
    #    System.IO 路径解析，路径超过 260 个字符就报「路径太长」，而且
    #    **删到一半就停下**，留下一棵半残的树 —— 下一次构建会踩到它。
    #    这里要删的恰恰是最深的那些：build\runtime 是 python-build-standalone
    #    解出来的（site-packages 里随便嵌套几层就超），PyInstaller 的
    #    build\pywork 更甚。给 .NET 的 API 传 `\\?\` 前缀可以绕开 MAX_PATH。
    # 2. **只读文件**。Directory.Delete 不像 Remove-Item -Force 那样替你摘掉
    #    只读属性，遇到只读文件直接抛 UnauthorizedAccessException。而 tar.exe
    #    解出来的文件是按归档里的权限位落的，可能是只读。
    #
    # 调用方在 $ErrorActionPreference = 'Stop' 下跑，所以删不掉会当场炸 ——
    # 这是故意的：删不干净比删慢更危险（会污染下一轮构建）。
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return }

    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith('\\?\')) { $full = '\\?\' + $full }

    if ([System.IO.File]::Exists($full)) {
        Clear-ReadOnly -Path $full
        [System.IO.File]::Delete($full)
        return
    }

    if (-not [System.IO.Directory]::Exists($full)) {
        throw "无法删除（既不是文件也不是目录）：$Path"
    }

    # **先直接删，只在被只读文件挡住时才去摘属性。**
    # 反过来写（无条件先把整棵树扫一遍）看起来更「稳」，其实是把上万个文件
    # 多遍历一次 —— 实测那一趟比删除本身还慢，而绝大多数文件根本不是只读的。
    try {
        [System.IO.Directory]::Delete($full, $true)
        return
    } catch [System.UnauthorizedAccessException] {
        # 落到这里说明有只读文件挡路。摘掉再删一次；上一轮已经删掉的部分不会重做。
        Write-Verbose "遇到只读文件，摘掉属性后重试：$Path"
    }
    Clear-ReadOnly -Path $full
    [System.IO.Directory]::Delete($full, $true)
}

# --------------------------------------------------------------------------- #
Write-Step '0/8 环境检查'
# --------------------------------------------------------------------------- #
# **不要直接读 $IsWindows**：它是 PowerShell 6+ 才有的自动变量，
# 在 Windows PowerShell 5.1 上不存在，而 `Set-StrictMode -Version Latest`
# 会把「读未定义的变量」直接变成终止性错误 —— 脚本还没开始干活就死了。
# 先用版本号短路，5.1 上右边根本不会被求值。
if ($PSVersionTable.PSVersion.Major -ge 6 -and -not $IsWindows) {
    Die '这个脚本只能在 Windows 上跑'
}
if ($env:OS -ne 'Windows_NT') {
    Die '这个脚本只能在 Windows 上跑'
}
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -ne 'AMD64') {
    Die "v1.0 的 Windows 包只支持 x64（当前架构：$arch）"
}
foreach ($tool in @('tar.exe', 'curl.exe', 'robocopy.exe')) {
    # **必须带 .exe**。在 Windows PowerShell 5.1 里 `curl` 是个**别名**，
    # 指向 Invoke-WebRequest —— 所以 `Get-Command curl` 永远成功，
    # 这个检查就成了摆设，真到下载那一步才炸（而那时已经跑了几分钟）。
    # 用 `Get-Command curl.exe` 查的才是 C:\Windows\System32\curl.exe 本身。
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Die "缺少系统工具：$tool（Windows 10 1803+ 自带）"
    }
}
Write-Host "  架构 $arch · Windows $([System.Environment]::OSVersion.Version) · 版本 $Version"

# **清掉从构建机 shell 继承来的 Python 环境变量。**
#
# 这个脚本跑的是**自带解释器**的构建，但子进程会原样继承父进程的环境：
#
# * `PYTHONPATH` —— 会插进内嵌 runtime 的 `sys.path`。构建机上装了什么都可能被
#   导进来，而且一个叫 `sitecustomize.py` 的东西会被**自动执行**。轻则污染依赖
#   解析、让 pip 报出莫名其妙的错；重则让 PyInstaller 的分析把不相干的模块
#   打进交付给用户的 exe 里。这不是假想问题：本项目自己的构建机就带着一个。
# * `PYTHONHOME` —— 更狠，直接把内嵌 runtime 的 `prefix` 整个改掉，
#   连标准库都找不到。
# * 用户级 site-packages（`%APPDATA%\Python\...`）—— 同理，构建机上恰好装过的包
#   会**遮蔽**内嵌 runtime 里的那一份，于是「本机构建通过、用户机器上炸」。
#
# 构建产出必须只由「本脚本 + 依赖清单」决定，不该随构建机的 shell 而变。
# 用 .NET 的 API 而不是 `Remove-Item env:`：后者是 cmdlet，会走 PowerShell 的
# 提供程序路径，对「删环境变量」这种纯内存操作是杀鸡用牛刀。
foreach ($name in @('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE')) {
    if ([System.Environment]::GetEnvironmentVariable($name)) {
        [System.Environment]::SetEnvironmentVariable($name, $null)
        Write-Host "  已清掉继承来的 $name（会污染内嵌解释器）"
    }
}
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'

if ($env:KF_PIP_INDEX_URL) {
    Write-Host "  pip 源：$($env:KF_PIP_INDEX_URL)"
}

# --------------------------------------------------------------------------- #
Write-Step '1/8 清理构建目录'
# --------------------------------------------------------------------------- #
if ($Clean) {
    Remove-Tree $BuildDir
    Write-Host "  [已清理] $BuildDir"
}
foreach ($dir in @($BuildDir, $DistDir, $StageRoot)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}
Remove-Tree $Stage

# --------------------------------------------------------------------------- #
Write-Step '2/8 准备内嵌 Python runtime'
# --------------------------------------------------------------------------- #
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
$PbsFile = Join-Path $CacheDir $PbsAsset
if (-not (Test-Path $PbsFile)) {
    Write-Host "  下载 $PbsAsset"
    $partial = "$PbsFile.part"
    # `-sS`：静音进度条，但真实错误照报。curl 的进度条是写 stderr 的，
    # 在 `$ErrorActionPreference = 'Stop'` 下会把脚本当场打断（见 Invoke-Native）。
    $curlCode = Invoke-Native -Program 'curl.exe' `
        -Arguments @('-fL', '-sS', '--retry', '3', '-o', $partial, $PbsUrl)
    if ($curlCode -ne 0) { Die "下载内嵌 runtime 失败：$PbsUrl" }
    Move-Item -Force $partial $PbsFile
} else {
    Write-Host "  命中缓存 $PbsAsset"
}

$Runtime = Join-Path $BuildDir 'runtime'
$Extract = Join-Path $BuildDir '.extract'
# 必须先把上一轮的 runtime 清掉：Move-Item 遇到已存在的目标目录时，
# 会把源目录**移进**目标目录里（变成 runtime\python\python.exe），
# 于是第二次不带 -Clean 的构建会在下面「找不到 python.exe」而失败。
if (Test-Path $Runtime) { Remove-Tree $Runtime }
Remove-Tree $Extract
New-Item -ItemType Directory -Force -Path $Extract | Out-Null
$tarCode = Invoke-Native -Program 'tar.exe' -Arguments @('-xzf', $PbsFile, '-C', $Extract)
if ($tarCode -ne 0) { Die '解压 runtime 失败' }
Move-Item (Join-Path $Extract 'python') $Runtime
Remove-Tree $Extract

$RuntimePy = Join-Path $Runtime 'python.exe'
if (-not (Test-Path $RuntimePy)) { Die "runtime 里没有 python.exe，下载可能不完整" }
$null = Invoke-Native -Program $RuntimePy -Arguments @('-V')

# --------------------------------------------------------------------------- #
Write-Step '3/8 安装核心依赖'
# --------------------------------------------------------------------------- #
$code = Invoke-Pip -Python $RuntimePy -Arguments @('-r', (Join-Path $PkgDir 'requirements-app-core.txt'))
if ($code -ne 0) { Die '核心依赖安装失败 —— 这是硬失败，不能降级' }
Write-Ok '核心依赖已装'

# --------------------------------------------------------------------------- #
Write-Step '4/8 安装可选能力（ASR / OCR）'
# --------------------------------------------------------------------------- #
if ($WithMedia) {
    # 装不上只降级、不中断构建 —— 用户仍能用链接/文本/手动粘贴。
    #
    # **但降级必须如实记账**：早先 macOS 版这里只看命令行开关，
    # 结果可选依赖装失败时 BUILD-INFO 仍写「已包含 ASR+OCR」，
    # 而包里根本没有那两个模块 —— 交付物在说谎。现在用变量记录真实结果，
    # 并在下面用 import 复核一遍。
    $code = Invoke-Pip -Python $RuntimePy -Arguments @('-r', (Join-Path $PkgDir 'requirements-app-media.txt'))
    if ($code -eq 0) {
        $MediaStatus = '已包含：ASR(faster-whisper) + OCR(pytesseract)'
        Write-Ok '已装：本地语音转写（faster-whisper）+ OCR（pytesseract）'
        Write-Host '  注：OCR 的实际引擎是系统里的 tesseract，未安装时自动停用'
    } else {
        $MediaStatus = '未包含（可选依赖安装失败）—— 应用照常可用，ASR/OCR 显示未启用'
        Write-Warn '可选能力未装成功 —— 应用照常可用，ASR/OCR 会显示「未启用」'
        Write-Warn '重跑一次构建通常就好了（构建期网络是唯一的单点故障）'
    }
} else {
    $MediaStatus = '未包含（-Minimal）'
    Write-Host '  -Minimal：跳过可选能力'
}

# 复核：说「已包含」就必须真的能 import，不能只信 pip 的返回码
if ($WithMedia) {
    foreach ($module in @('faster_whisper', 'av', 'pytesseract', 'PIL')) {
        $importCode = Invoke-Native -Program $RuntimePy -Arguments @('-c', "import $module") -Quiet
        if ($importCode -ne 0) {
            $MediaStatus = "未包含（复核失败：$module 导不进来）—— ASR/OCR 显示未启用"
            Write-Warn "复核发现 $module 缺失，按「未包含」记账"
            break
        }
    }
}

# 清掉测试与缓存，只留运行时需要的东西（体积与攻击面都小一圈）
$CleanupScript = @"
import pathlib, shutil, sys
site = pathlib.Path(sys.prefix) / 'Lib' / 'site-packages'
removed = 0
for pattern in ('**/tests', '**/test', '**/__pycache__'):
    for path in site.glob(pattern):
        if path.is_dir() and 'site-packages' in str(path):
            try:
                shutil.rmtree(path)
                removed += 1
            except OSError:
                pass
print(f'  清掉 {removed} 个测试/缓存目录')
"@
# 清理失败不该中断构建（最多是包大一点）
$null = Invoke-Native -Program $RuntimePy -Arguments @('-c', $CleanupScript)

# --------------------------------------------------------------------------- #
Write-Step '5/8 组装目录树'
# --------------------------------------------------------------------------- #
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

# 后端原样拷进去（排除测试/数据/凭据）
# robocopy 的退出码 0-7 都是成功（1=有文件被复制），8+ 才是失败
$rc = Invoke-Native -Program 'robocopy.exe' -Arguments @(
    (Join-Path $RepoRoot 'backend'), (Join-Path $Stage 'backend'),
    '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP',
    '/XD', 'tests', '.pytest_tmp', '.pytest_cache', 'data', '__pycache__',
    '/XF', '.env', '*.db', '*.sqlite3', '*.pyc'
) -Quiet
if ($rc -ge 8) { Die "拷贝 backend 失败（robocopy 退出码 $rc）" }

# 内嵌 runtime
$rc = Invoke-Native -Program 'robocopy.exe' -Arguments @(
    $Runtime, (Join-Path $Stage 'runtime'), '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'
) -Quiet
if ($rc -ge 8) { Die "拷贝 runtime 失败（robocopy 退出码 $rc）" }

# 启动器源码（exe 与 .cmd 都要用）
$rc = Invoke-Native -Program 'robocopy.exe' -Arguments @(
    (Join-Path $PkgDir 'launcher'), (Join-Path $Stage 'launcher'),
    '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/XD', '__pycache__'
) -Quiet
if ($rc -ge 8) { Die "拷贝 launcher 失败（robocopy 退出码 $rc）" }

# 配置模板也带上：出问题时对照用
$StagePkg = Join-Path $Stage 'packaging'
New-Item -ItemType Directory -Force -Path $StagePkg | Out-Null
Copy-Item (Join-Path $PkgDir 'requirements-app-core.txt') $StagePkg
Copy-Item (Join-Path $PkgDir 'requirements-app-media.txt') $StagePkg

# --------------------------------------------------------------------------- #
Write-Step '6/8 生成图标与 exe'
# --------------------------------------------------------------------------- #
# 构建工具装在独立 venv 里，**不污染内嵌 runtime** —— 否则 PyInstaller
# 自己会被打进包里，用户拿到一个 200MB 的 exe 和他的应用毫无关系。
$Tools = Join-Path $BuildDir '.tools'
$ToolsPy = Join-Path $Tools 'Scripts\python.exe'

# **判据是「pip 真的能用」，不是「python.exe 在不在」。**
# venv 是分步建的：目录与 `Scripts\python.exe` 先落盘，`ensurepip` 那一步在后面。
# ensurepip 失败时目录**已经存在**了 —— 只看 python.exe 会把半成品当成品，
# 下一次构建直接跳过创建，然后在 `$ToolsPy -m pip install` 那里报一个
# 跟真实原因（venv 根本没建成）毫不相干的错。
$toolsOk = $false
if (Test-Path $ToolsPy) {
    $toolsOk = (Invoke-Native -Program $ToolsPy -Arguments @('-m', 'pip', '--version') -Quiet) -eq 0
    if (-not $toolsOk) {
        Write-Warn 'build\.tools 里有一份不完整的 venv（多半是上次 ensurepip 失败留下的），重建'
    }
}
if (-not $toolsOk) {
    Remove-Tree $Tools
    $venvCode = Invoke-Native -Program $RuntimePy -Arguments @('-m', 'venv', $Tools)
    if ($venvCode -ne 0) { Die '创建构建工具 venv 失败' }
    # venv 返回 0 不等于 pip 真的装好了 —— 复核一次，否则下面的报错指不到真因
    if ((Invoke-Native -Program $ToolsPy -Arguments @('-m', 'pip', '--version') -Quiet) -ne 0) {
        Die '构建工具 venv 建好了，但里面没有可用的 pip'
    }
}
$code = Invoke-Pip -Python $ToolsPy -Arguments @('pyinstaller', 'pillow')
if ($code -ne 0) { Die '构建工具（pyinstaller / pillow）安装失败' }

# 图标：从 macOS 版同一张 1024 母版生成多分辨率 .ico —— 一处美术资产，两个平台共用
$IconPath = Join-Path $BuildDir 'AppIcon.ico'
$Master = Join-Path $RepoRoot 'packaging\macos\assets\AppIcon-1024.png'
if (-not (Test-Path $Master)) { Die "缺少图标母版：$Master" }
$IconScript = Join-Path $BuildDir 'make_icon.py'
$IconContent = @"
from PIL import Image

master = Image.open(r'$Master').convert('RGBA')
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
master.save(r'$IconPath', format='ICO', sizes=sizes)
print('  图标已生成：' + r'$IconPath')
"@
# 同样是 Python 源码：**不带 BOM**，免得 `Set-Content -Encoding UTF8` 塞一个 U+FEFF 进去
[System.IO.File]::WriteAllText(
    $IconScript, $IconContent, (New-Object System.Text.UTF8Encoding($false))
)
$iconCode = Invoke-Native -Program $ToolsPy -Arguments @($IconScript)
if ($iconCode -ne 0) { Die '生成 .ico 失败' }

# 版本资源：Windows 上对应 macOS 的 Info.plist —— 属性页里能看到是谁、什么版本
$VersionFile = Join-Path $BuildDir 'version_info.txt'
$VersionParts = @($Version.Split('.') + @('0', '0', '0'))[0..3]
$VerTuple = ($VersionParts | ForEach-Object { [int]$_ }) -join ', '
$VersionContent = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($VerTuple),
    prodvers=($VerTuple),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'KnowledgeFlow'),
        StringStruct('FileDescription', 'KnowledgeFlow 桌面启动器'),
        StringStruct('FileVersion', '$Version'),
        StringStruct('InternalName', 'KnowledgeFlow'),
        StringStruct('OriginalFilename', 'KnowledgeFlow.exe'),
        StringStruct('ProductName', 'KnowledgeFlow'),
        StringStruct('ProductVersion', '$Version'),
        StringStruct('LegalCopyright', 'MIT License')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"@
# **这份文件必须不带 BOM**：PyInstaller 是把它的内容当 Python 表达式 eval 的，
# 开头的 U+FEFF 会直接变成语法错误（报错还指不到编码上）。
# 而 PowerShell 5.1 的 `Set-Content -Encoding UTF8` 恰恰会加 BOM —— 那是它的历史
# 行为，跟「UTF-8」这个名字给人的预期不一致。所以这里绕开 cmdlet，直接走 .NET。
[System.IO.File]::WriteAllText(
    $VersionFile, $VersionContent, (New-Object System.Text.UTF8Encoding($false))
)

$Entry = Join-Path $PkgDir 'launcher\run.py'
$pyiArgs = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--clean', '--onefile', '--noconsole',
    '--name', $AppName,
    '--icon', $IconPath,
    '--version-file', $VersionFile,
    '--paths', (Join-Path $PkgDir 'launcher'),
    '--distpath', (Join-Path $BuildDir 'pydist'),
    '--workpath', (Join-Path $BuildDir 'pywork'),
    '--specpath', (Join-Path $BuildDir 'pyspec'),
    $Entry
)
$pyiCode = Invoke-Native -Program $ToolsPy -Arguments $pyiArgs
if ($pyiCode -ne 0) { Die 'PyInstaller 构建失败' }

$BuiltExe = Join-Path $BuildDir "pydist\$AppName.exe"
if (-not (Test-Path $BuiltExe)) { Die "PyInstaller 没有产出 exe：$BuiltExe" }
Copy-Item $BuiltExe (Join-Path $Stage "$AppName.exe")
Write-Ok "$AppName.exe（$([math]::Round((Get-Item $BuiltExe).Length / 1MB, 1)) MB）"

# 命令行入口。无控制台的 exe 里 print() 没有出口，--status/--stop/--check
# 本来就是给人看输出的，所以单独给一份走内嵌解释器的 .cmd。
# **必须用 %~dp0 而不是 %CD%**：用户可能从任意工作目录调用它。
#
# 这份 .cmd 有两条硬约束，都是实测出来的（原来两条都踩了）：
#
# 1. **行尾必须是 CRLF，而且要显式拼。**
#    cmd.exe **不认 LF-only 的批处理**：它会把注释行按错误的边界切开，把 rem
#    后面的片段当命令去执行。实测（同一份内容，只换行尾）：
#        LF   → 用户看到三行「'-status' 不是内部或外部命令」这类噪音
#        CRLF → 干净
#    而**不能靠 here-string 的行尾** —— 它跟着 .ps1 自己的行尾走，仓库里这份
#    .ps1 是 LF（git 的 autocrlf 归一化过），从干净克隆构建出来必然又是 LF。
#    所以这里用「行数组 + -join "`r`n"」显式指定。
#
# 2. **每一行都必须是纯 ASCII。**
#    批处理是**按字节**解析的：GBK 汉字的次字节可能正好落在 cmd 的特殊字符上
#    （`|` 0x7C、`&` 0x26、`<` 0x3C、`>` 0x3E、`^` 0x5E），那时行会被从中间
#    切开，跟控制台码页无关。这次的中文注释恰好没踩上（改成 CRLF 就干净了），
#    但那是运气。注释写成英文，就没有这个运气成分。
#    （说明放在这里而不是 .cmd 里 —— 排障看的是构建脚本，不是那份生成的批处理。）
$CmdPath = Join-Path $Stage "$AppName.cmd"
$CmdLines = @(
    '@echo off',
    "rem $AppName command-line entry (--status / --stop / --check / --setup)",
    "rem Runs via the bundled interpreter; to launch by double-click use $AppName.exe.",
    'setlocal',
    '"%~dp0runtime\python.exe" "%~dp0launcher\run.py" %*',
    'exit /b %ERRORLEVEL%'
)
Set-Content -Path $CmdPath -Encoding OEM -NoNewline -Value (($CmdLines -join "`r`n") + "`r`n")

# --------------------------------------------------------------------------- #
Write-Step '7/8 冒烟测试（用打包好的产物真的跑一次）'
# --------------------------------------------------------------------------- #
$SmokeHome = Join-Path $BuildDir 'smoke-home'
Remove-Tree $SmokeHome
New-Item -ItemType Directory -Force -Path $SmokeHome | Out-Null

$SavedLocalAppData = $env:LOCALAPPDATA
$SavedIoEncoding = $env:PYTHONIOENCODING
$SavedUtf8Mode = $env:PYTHONUTF8
try {
    # 把用户数据目录指到临时位置：冒烟测试不该在真实 %LOCALAPPDATA% 里留下东西
    $env:LOCALAPPDATA = $SmokeHome

    # **冒烟测试要像用户的机器，不能像构建机。**
    # 构建机上常设 PYTHONIOENCODING=utf-8 / PYTHONUTF8=1（本脚本第 0 步就设了
    # PYTHONUTF8），而这两个变量会让内嵌解释器绕过 GBK 输出问题 ——
    # 于是「中文 Windows 上 --check 抛 UnicodeEncodeError」这个真实缺陷
    # 在构建机上永远测不出来。实测正是这样漏过去的：exe 在用户的 GBK 控制台上
    # 退出码 1、stdout 一个字节都没有，而这里的冒烟测试却"通过"了。
    #
    # 用 `$env:X = $null` 而不是 `Remove-Item Env:X`：后者会被
    # `test_build_script_never_calls_remove_item` 拦下 —— 那条守卫是给**删目录**
    # 立的（Remove-Item -Recurse 会留下半棵残树），没必要为一个环境变量破例。
    # 空串的 PYTHONIOENCODING 与未设等价（实测），所以两种语义都安全。
    $env:PYTHONIOENCODING = $null
    $env:PYTHONUTF8 = $null

    # ① 冻结后的 exe：验证它**能找到自己的安装目录**。
    #    这一条专治一个坑：PyInstaller 冻结后 __file__ 指向临时解包目录，
    #    若启动器拿它往上找安装目录，会静默退化成「开发模式」而找不到 backend。
    #
    #    用调用运算符 `&` 而不是 Start-Process：PowerShell 会等原生程序退出，
    #    退出码落在 $LASTEXITCODE 里。Start-Process 会拉起一个脱离的子进程，
    #    在受限环境（安全策略禁止启动新进程）里会被直接拦掉，而且拿不到输出。
    #
    #    **必须 -Capture**：GUI 子系统的 exe 从控制台启动时会继承控制台句柄，
    #    所以输出既能被捕获、也按 ANSI 码页（中文机器上是 GBK）编码 ——
    #    正好是真实用户遇到的情形。只看退出码不够：退出码 1 既可能是
    #    「找不到安装目录」也可能是「编码炸了」，不给输出就一点线索都没有。
    Write-Host '  用打包好的 exe 做环境自检：'
    $exePath = Join-Path $Stage "$AppName.exe"
    $exeExit = Invoke-Native -Program $exePath -Arguments @('--check') -Capture
    $exeOut = $script:NativeOutput -join "`n"
    if ($exeExit -ne 0) {
        Die "exe 自检失败（退出码 $exeExit）`n实际输出：`n$exeOut"
    }
    foreach ($needle in @('内置运行环境', '后端程序', '已就绪')) {
        if ($exeOut -notmatch [regex]::Escape($needle)) {
            Die "exe 自检输出里缺少「$needle」—— 冻结后可能没找到安装目录。实际输出：`n$exeOut"
        }
    }

    # ② console.log：双击启动时用户**看不到任何输出**，这份日志是唯一线索，
    #    所以启动器必须**无条件**写它（不管有没有控制台）。
    $ConsoleLog = Join-Path $SmokeHome "$AppName\logs\console.log"
    if (-not (Test-Path $ConsoleLog)) {
        Die "exe 没有产出 console.log —— 启动器把日志丢了（双击启动时用户将没有任何线索）"
    }
    $logText = Get-Content $ConsoleLog -Raw -Encoding UTF8
    foreach ($needle in @('内置运行环境', '已就绪')) {
        if ($logText -notmatch [regex]::Escape($needle)) {
            Die "console.log 里缺少「$needle」。实际内容：`n$logText"
        }
    }
    Write-Ok 'exe 自检通过（能找到自己的 runtime 与 backend，输出与日志都在）'

    # ③ .cmd 路径：走内嵌解释器，输出能直接看到
    #    直接调用 .cmd（PowerShell 能跑批处理），不绕一层 cmd.exe ——
    #    多一层壳只会多一处被安全策略拦下的地方。
    Write-Host '  用 .cmd 走一遍同样的自检：'
    $cmdCode = Invoke-Native -Program (Join-Path $Stage "$AppName.cmd") -Arguments @('--check') -Capture
    $cmdOut = $script:NativeOutput -join "`n"
    if ($cmdCode -ne 0) { Die ".cmd 自检失败（退出码 $cmdCode）`n$cmdOut" }
    if ($cmdOut -notmatch '内置运行环境') { Die ".cmd 自检输出异常：`n$cmdOut" }
    Write-Ok '.cmd 自检通过'

    # ④ .cmd 自身的体检。它是**本脚本生成**的，所以错了就该当场失败 ——
    #    别等用户双击时看到几行「'xxx' 不是内部或外部命令」。
    #    按 latin1（逐字节）读：用 UTF-8 读会把多字节字符变成替换字符，查不出原样。
    $cmdFile = Join-Path $Stage "$AppName.cmd"
    $cmdRaw = [System.IO.File]::ReadAllText($cmdFile, [System.Text.Encoding]::GetEncoding(28591))
    $bareLf = ([regex]::Matches($cmdRaw, "(?<!`r)`n")).Count
    $crlf = ([regex]::Matches($cmdRaw, "`r`n")).Count
    $nonAscii = ([regex]::Matches($cmdRaw, "[^\x00-\x7F]")).Count
    if ($crlf -eq 0) { Die "$AppName.cmd 里一个 CRLF 都没有 —— 行尾不对" }
    if ($bareLf -ne 0) {
        Die "$AppName.cmd 有 $bareLf 个裸 LF 行尾 —— cmd.exe 不认 LF-only 批处理，会把注释当命令执行"
    }
    if ($nonAscii -ne 0) {
        Die "$AppName.cmd 有 $nonAscii 个非 ASCII 字节 —— 多字节字符的次字节可能落在 cmd 的特殊字符上"
    }
    Write-Ok "$AppName.cmd 体检通过（$crlf 个 CRLF、纯 ASCII）"
} finally {
    $env:LOCALAPPDATA = $SavedLocalAppData
    # **只在原来真的有值时才写回**：`$env:X = $null` 的语义在不同 PowerShell 版本上
    # 不一致（有的删掉变量、有的写成空串），而「本来没设过」就该保持没设过 ——
    # 这一步是为了**原样还原环境**，不是在防某个具体的坑。
    if ($null -ne $SavedIoEncoding) { $env:PYTHONIOENCODING = $SavedIoEncoding }
    if ($null -ne $SavedUtf8Mode) { $env:PYTHONUTF8 = $SavedUtf8Mode }
}

# --------------------------------------------------------------------------- #
Write-Step '8/8 打 ZIP'
# --------------------------------------------------------------------------- #
if (-not $NoZip) {
    Remove-Tree $ZipPath
    # 用 bsdtar（Windows 自带）而不是 Compress-Archive：后者对几百 MB / 上万个文件
    # 极慢，而且有 2GB 上限。tar 的 -a 会按扩展名自动选 zip 格式。
    $zipCode = Invoke-Native -Program 'tar.exe' `
        -Arguments @('-a', '-c', '-f', $ZipPath, '-C', $StageRoot, $StageName)
    if ($zipCode -ne 0) {
        Write-Warn 'tar 打包失败，回退到 Compress-Archive（会慢一些）'
        Compress-Archive -Path $Stage -DestinationPath $ZipPath -CompressionLevel Optimal
    }
    $zipMb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
    Write-Ok "$ZipPath（$zipMb MB）"
} else {
    Write-Host '  -NoZip：跳过'
}

# 构建信息：交付时一眼能看清「这个包是什么、怎么来的」
$StageSize = [math]::Round(((Get-ChildItem $Stage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
$InfoPath = Join-Path $DistDir 'BUILD-INFO-windows.txt'
@"
KnowledgeFlow $Version · Windows x64（便携版）
构建时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
构建机：$arch / Windows $([System.Environment]::OSVersion.Version)
内嵌 Python：$(& $RuntimePy -V 2>&1)
可选能力：$MediaStatus
签名：无（未做代码签名，首次运行可能被 SmartScreen 拦一下）
目录体积：$StageSize MB
ZIP：$(if (-not $NoZip) { "$zipMb MB" } else { '已跳过' })
数据目录：%LOCALAPPDATA%\$AppName\
入口：$AppName.exe（双击）/ $AppName.cmd（命令行）
"@ | Set-Content -Encoding UTF8 $InfoPath

Write-Host ''
Write-Host '构建完成' -ForegroundColor Green
if (-not $NoZip) { Write-Host "  便携包：$ZipPath" }
Write-Host "  目录  ：$Stage"
Write-Host "  构建信息：$InfoPath"
