# Captures real screenshots of Adarna's three windows (main window,
# Suggestions window, overlay) for the landing page, using PrintWindow so
# each capture is just that one window, not a full-desktop grab. Run
# *while* wsl_app/research/day31_landing_screenshots.py (the standalone
# smoke-test driver, no mocks) is showing those windows -- it prints
# "Adarna", "Adarna Suggestions", and "Adarna Overlay" as their titles.
#
# Usage (from a WSL shell, via interop):
#   powershell.exe -File design_preferences\capture_screenshots.ps1 -OutDir <windows-path-to-screenshots-dir>

param(
    [Parameter(Mandatory = $true)]
    [string]$OutDir
)

Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public class Win32Capture {
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);

    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@

function Find-WindowByTitle {
    param([string]$Title)

    $script:foundHwnd = [IntPtr]::Zero
    $callback = {
        param($hWnd, $lParam)
        $length = [Win32Capture]::GetWindowTextLength($hWnd)
        if ($length -gt 0) {
            $sb = New-Object System.Text.StringBuilder ($length + 1)
            [Win32Capture]::GetWindowText($hWnd, $sb, $sb.Capacity) | Out-Null
            if ($sb.ToString() -eq $Title -and [Win32Capture]::IsWindowVisible($hWnd)) {
                $script:foundHwnd = $hWnd
                return $false
            }
        }
        return $true
    } -as [Win32Capture+EnumWindowsProc]

    [Win32Capture]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
    return $script:foundHwnd
}

function Save-WindowScreenshot {
    param([string]$Title, [string]$OutFile)

    $hwnd = Find-WindowByTitle -Title $Title
    if ($hwnd -eq [IntPtr]::Zero) {
        Write-Warning "Window not found: $Title"
        return $false
    }

    $rect = New-Object Win32Capture+RECT
    [Win32Capture]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -le 0 -or $height -le 0) {
        Write-Warning "Window has no visible area: $Title"
        return $false
    }

    $bitmap = New-Object System.Drawing.Bitmap $width, $height
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $hdc = $graphics.GetHdc()
    # Flag 2 = PW_RENDERFULLCONTENT -- needed for modern GPU-composited
    # windows (Qt/PySide6 included); plain PrintWindow (flag 0) can come
    # back blank on those.
    [Win32Capture]::PrintWindow($hwnd, $hdc, 2) | Out-Null
    $graphics.ReleaseHdc($hdc)
    $graphics.Dispose()

    $bitmap.Save($OutFile, [System.Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
    Write-Host "Saved: $OutFile"
    return $true
}

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir | Out-Null
}

Save-WindowScreenshot -Title "Adarna" -OutFile (Join-Path $OutDir "main_window.png")
Save-WindowScreenshot -Title "Adarna Suggestions" -OutFile (Join-Path $OutDir "suggestions_window.png")
Save-WindowScreenshot -Title "Adarna Overlay" -OutFile (Join-Path $OutDir "overlay.png")
