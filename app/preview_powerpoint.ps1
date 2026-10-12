# Export every slide of a .pptx as PNG using the locally installed Microsoft PowerPoint (COM).
# Called by app/preview.py with -File, so paths are passed as arguments, never interpolated.
param(
    [Parameter(Mandatory = $true)][string]$Pptx,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [int]$Width = 1280
)
$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject PowerPoint.Application
$pres = $null
try {
    # ReadOnly = msoTrue, Untitled = msoFalse, WithWindow = msoFalse
    $pres = $app.Presentations.Open($Pptx, -1, 0, 0)
    $height = [int]([math]::Round($Width * $pres.PageSetup.SlideHeight / $pres.PageSetup.SlideWidth))
    $i = 0
    foreach ($slide in $pres.Slides) {
        $i++
        $slide.Export((Join-Path $OutDir ('slide-{0:D2}.png' -f $i)), 'PNG', $Width, $height)
    }
    Write-Output $i
}
finally {
    if ($pres) { $pres.Close() }
    # Only quit PowerPoint if we were its only user (never close the user's open decks).
    if ($app.Presentations.Count -eq 0) { $app.Quit() }
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($app)
}
