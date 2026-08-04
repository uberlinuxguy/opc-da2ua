# Build script for OPC DA→UA Gateway (32-bit Python 3.8)
# Uses Python 3.8-32 + PySide2 (Qt 5.15) + downgraded dependencies
# Creates a standalone Windows executable using Nuitka

$ErrorActionPreference = "Stop"

# Check for arguments
$Clean = $args -contains "clean"
$VenvOnly = $args -contains "venv"

Write-Host "========================================" -ForegroundColor Cyan
if ($VenvOnly) {
    Write-Host "  OPC DA-to-UA Gateway (Venv Setup Only)" -ForegroundColor Cyan
} else {
    Write-Host "  OPC DA-to-UA Gateway Build" -ForegroundColor Cyan
}
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$VENV_DIR = "venv"
$PYTHON_EXE = "$VENV_DIR\Scripts\python.exe"
$NUITKA_VERSION = "1.8.0"  # Last version with good Python 3.8 support

# ------------------------------------------------------------------
# Step 1: Install Python 3.8-32 if not present
# ------------------------------------------------------------------
Write-Host "[1/5] Checking for 32-bit Python 3.8..." -ForegroundColor Yellow

# Only look for 32-bit Python 3.8 (required for OPC DA COM compatibility)
$python38_paths = @(
    "$env:LOCALAPPDATA\Programs\Python\Python38-32\python.exe",
    "C:\Python38-32\python.exe",
    "C:\Program Files\Python38-32\python.exe"
)

$python38_exe = $null
foreach ($p in $python38_paths) {
    if (Test-Path $p) {
        $python38_exe = $p
        break
    }
}

if (-not $python38_exe) {
    Write-Host ""
    Write-Host "ERROR: 32-bit Python 3.8 not found on this system." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install 32-bit Python 3.8 from:" -ForegroundColor Yellow
    Write-Host "  https://www.python.org/ftp/python/3.8.19/python-3.8.19.exe" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "IMPORTANT: Select 'Install for all users' and check" -ForegroundColor Yellow
    Write-Host "'Add Python 3.8 to PATH' during installation." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

Write-Host "  Found: $python38_exe" -ForegroundColor Green

# ------------------------------------------------------------------
# Step 2: Create virtual environment
# ------------------------------------------------------------------
Write-Host ""
Write-Host "[2/5] Setting up virtual environment..." -ForegroundColor Yellow

if ($Clean) {
    Write-Host "  Removing existing venv..." -ForegroundColor Gray
    Remove-Item -Recurse -Force $VENV_DIR -ErrorAction SilentlyContinue
    $clean_dirs = @("dist", "build", "opc_da2ua.build", "opc_da2ua.dist")
    foreach ($dir in $clean_dirs) {
        if (Test-Path $dir) {
            Write-Host "  Removing $dir..." -ForegroundColor Gray
            Remove-Item -Recurse -Force $dir
        }
    }
}

if (Test-Path $VENV_DIR) {
    Write-Host "  Reusing existing venv" -ForegroundColor Gray
} else {
    & $python38_exe -m venv $VENV_DIR
    Write-Host "  Created: $VENV_DIR" -ForegroundColor Green
}

# ------------------------------------------------------------------
# Step 3: Install dependencies
# ------------------------------------------------------------------
Write-Host ""
Write-Host "[3/5] Installing dependencies (this may take a few minutes)..." -ForegroundColor Yellow

# Upgrade pip first
& $PYTHON_EXE -m pip install --upgrade pip setuptools wheel --quiet

# Install requirements
& $PYTHON_EXE -m pip install -r requirements.txt --quiet

# Install Nuitka
Write-Host "  Installing Nuitka $NUITKA_VERSION..." -ForegroundColor Gray
& $PYTHON_EXE -m pip install "nuitka==$NUITKA_VERSION" --quiet

Write-Host "  Dependencies installed." -ForegroundColor Green

# If "venv" argument was passed, stop here
if ($VenvOnly) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  Virtual environment ready!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "To activate the venv:" -ForegroundColor Yellow
    Write-Host "  .\$VENV_DIR\Scripts\Activate.ps1" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "To run the application:" -ForegroundColor Yellow
    Write-Host "  .\$VENV_DIR\Scripts\python.exe opc_da2ua.py" -ForegroundColor Cyan
    Write-Host ""
    exit 0
}

# ------------------------------------------------------------------
# Step 4: Clean previous build (only if "clean" is passed)
# ------------------------------------------------------------------
if ($Clean) {
    Write-Host ""
    Write-Host "[4/5] Cleaning previous build artifacts..." -ForegroundColor Yellow

    $clean_dirs = @("dist", "build", "opc_da2ua.build", "opc_da2ua.dist")
    foreach ($dir in $clean_dirs) {
        if (Test-Path $dir) {
            Remove-Item -Recurse -Force $dir
            Write-Host "  Removed: $dir" -ForegroundColor Gray
        }
    }
} else {
    Write-Host ""
    Write-Host "[4/5] Skipping cleanup (pass 'clean' to remove old artifacts)" -ForegroundColor Gray
}

# ------------------------------------------------------------------
# Step 5: Build with Nuitka
# ------------------------------------------------------------------
Write-Host ""
Write-Host "[5/5] Building with Nuitka..." -ForegroundColor Yellow
Write-Host ""

& $PYTHON_EXE -m nuitka `
    --standalone `
    --output-dir=dist `
    --output-filename=opc_da2ua.exe `
    --include-package=PySide2 `
    --include-package=asyncua `
    --nofollow-import-to=asyncua.server.standard_address_space `
    --nofollow-import-to=asyncua.ua.uaprotocol_auto `
    --nofollow-import-to=asyncua.ua.object_ids `
    --include-data-files="venv/lib/site-packages/asyncua/ua/uaprotocol_auto.py=asyncua/ua/uaprotocol_auto.py" `
    --include-data-files="venv/lib/site-packages/asyncua/ua/object_ids.py=asyncua/ua/object_ids.py" `
    --include-data-files="venv/lib/site-packages/asyncua/server/standard_address_space/__init__.py=asyncua/server/standard_address_space/__init__.py" `
    --include-data-files="venv/lib/site-packages/asyncua/server/standard_address_space/standard_address_space.py=asyncua/server/standard_address_space/standard_address_space.py" `
    --include-data-files="venv/lib/site-packages/asyncua/server/standard_address_space/standard_address_space_services.py=asyncua/server/standard_address_space/standard_address_space_services.py" `
    --include-package=OpenOPC `
    --include-package=cryptography `
    --include-package=win32com `
    --include-module=pythoncom `
    --include-module=pywintypes `
    --include-module=win32api `
    --include-module=win32con `
    --enable-plugin=pyside2 `
    --show-scons `
    opc_da2ua.py

# ------------------------------------------------------------------
# Results
# ------------------------------------------------------------------
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan

$exe_path = "dist\opc_da2ua.dist\opc_da2ua.exe"
if (Test-Path $exe_path) {
    Write-Host "  BUILD SUCCESSFUL!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Output: $exe_path" -ForegroundColor Cyan
    
    $files = Get-ChildItem -Path "dist\opc_da2ua.dist" -Recurse -File
    $size = [math]::Round(($files | Measure-Object -Property Length -Sum).Sum / 1MB, 2)
    Write-Host "Files: $($files.Count)" -ForegroundColor Yellow
    Write-Host "Size: ${size} MB" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "This executable is compatible with:" -ForegroundColor Green
    Write-Host "  - Windows Server 2012 R2" -ForegroundColor White
    Write-Host "  - Windows 8.1" -ForegroundColor White
    Write-Host "  - Windows 10 / 11" -ForegroundColor White
    Write-Host "  - Windows Server 2016 / 2019 / 2022" -ForegroundColor White
    Write-Host ""
    Write-Host "To deploy, copy the entire 'dist\opc_da2ua.dist' folder" -ForegroundColor Cyan
    Write-Host "to the target machine and run opc_da2ua.exe" -ForegroundColor Cyan
} else {
    Write-Host "  BUILD FAILED!" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Check the output above for errors." -ForegroundColor Yellow
}