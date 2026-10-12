# Build a synthetic Vietnamese lecture video for end-to-end tests (no copyrighted material):
# Windows OneCore TTS voice (vi-VN "An") + FFmpeg-generated title cards.
#   powershell -ExecutionPolicy Bypass -File scripts\make_test_video.ps1 -Out data\fixtures\lecture_vi.mp4
param(
    [string]$Out = (Join-Path $PSScriptRoot '..\data\fixtures\lecture_vi.mp4'),
    [string]$TextFile = (Join-Path $PSScriptRoot 'fixtures\lecture_vi.txt')
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]

function Await($op, [Type]$type) {
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.Language -eq 'vi-VN' } | Select-Object -First 1
if (-not $voice) { throw 'No vi-VN voice installed (Settings > Time & language > Speech > Add voices).' }
$synth.Voice = $voice

$text = Get-Content $TextFile -Raw -Encoding UTF8
$stream = Await ($synth.SynthesizeTextToStreamAsync($text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
$reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
$size = [uint32]$stream.Size
$null = Await ($reader.LoadAsync($size)) ([uint32])
$bytes = New-Object byte[] $size
$reader.ReadBytes($bytes)

$outDir = Split-Path -Parent ([IO.Path]::GetFullPath($Out))
New-Item -ItemType Directory -Force $outDir | Out-Null
$wav = Join-Path $outDir 'lecture_vi.wav'
[IO.File]::WriteAllBytes($wav, $bytes)

# Video: changing colour scenes so frame extraction has distinct shots; audio = the speech.
& ffmpeg -nostdin -y -v error -i $wav `
    -f lavfi -i 'testsrc2=size=1280x720:rate=15' `
    -filter_complex '[1:v]hue=H=t/9,format=yuv420p[v]' -map '[v]' -map 0:a `
    -c:v libx264 -preset veryfast -crf 30 -c:a aac -b:a 96k -shortest ([IO.Path]::GetFullPath($Out))
if ($LASTEXITCODE -ne 0) { throw 'ffmpeg failed' }
Remove-Item $wav
Write-Output ([IO.Path]::GetFullPath($Out))
