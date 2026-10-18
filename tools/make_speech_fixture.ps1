# Generate real spoken audio with the Windows built-in TTS engine (SAPI).
# Used to exercise the transcription path end to end: the synthetic tone
# fixture in make_fixture.py has no recognisable speech by design, so the
# ASR / word-timestamp / caption path needs actual words to run against.
#
# Usage:  powershell -NoProfile -File tools\make_speech_fixture.ps1

param(
    [string]$Out = "fixtures\speech.wav",
    [double]$Rate = 0
)

$ErrorActionPreference = "Stop"

try {
    Add-Type -AssemblyName System.Speech
} catch {
    Write-Error "System.Speech unavailable. Install the Windows TTS voices."
    exit 1
}

$dir = Split-Path -Parent $Out
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

# A short monologue with a clear narrative arc, a question, an exclamation and
# a proper ending -- enough variety to give the ranker something to chew on.
$script = @(
    "So I was thinking about what actually makes a clip work.",
    "You know, everyone asks me where I get these ideas.",
    "Honestly, it is mostly just terrible luck.",
    "But here is the thing that surprised me most.",
    "The best moments are rarely the ones you plan for.",
    "They are the ones where somebody forgets to be careful.",
    "What if the whole point was just to leave the door open?",
    "That is genuinely wild.",
    "Okay, anyway, back to the boring part."
) -join " "

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = $Rate
$voice = $synth.GetInstalledVoices() |
    Where-Object { $_.Enabled } |
    Select-Object -First 1

if ($voice) {
    Write-Host "voice: $($voice.VoiceInfo.Name)"
    $synth.SelectVoice($voice.VoiceInfo.Name)
}

# Write with the SAPI default format. It is not 16 kHz mono, but the pipeline
# always runs its own `ffmpeg -ar 16000 -ac 1` decode, so the rate here does not
# matter -- and specifying SpeechAudioFormatInfo explicitly needs a constructor
# overload that is not present on every SAPI build.
$synth.SetOutputToWaveFile((Join-Path (Get-Location) $Out))
$synth.Speak($script)
$synth.SetOutputToNull()
$synth.Dispose()

Write-Host "wrote $Out"