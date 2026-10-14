# Export every slide of a .pptx as PNG (or, with -PdfOut, the whole deck as one vector PDF with one page per
# slide) using the locally installed Microsoft PowerPoint (COM).
# Called by app/preview.py with -File, so paths are passed as arguments, never interpolated.
param(
    [Parameter(Mandatory = $true)][string]$Pptx,
    [string]$OutDir = '',
    [int]$Width = 1280,
    [string]$PdfOut = ''
)
$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject PowerPoint.Application
$pres = $null
try {
    # ReadOnly = msoTrue, Untitled = msoFalse, WithWindow = msoFalse
    $pres = $app.Presentations.Open($Pptx, -1, 0, 0)
    if ($PdfOut) {
        # ppSaveAsPDF = 32: one page per slide, text stays text, fonts embedded. (ExportAsFixedFormat cannot be
        # called from PowerShell: its optional Variant arguments fail to bind.)
        $pres.SaveAs($PdfOut, 32)
        Write-Output $pres.Slides.Count
        return
    }
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
