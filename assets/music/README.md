# Drop your own background music here and Clipper will use it instead of
# synthesising a bed.
#
# Any format ffmpeg can read: .mp3 .wav .m4a .flac .ogg .opus .aac
#
# Why this folder exists: Clipper synthesises its default music from
# oscillators, so there is no audio asset in the repository and nothing to
# license. If you want a specific track, put it here and it takes priority.
#
# Tracks are picked deterministically per clip, so re-running a job produces
# the same music rather than reshuffling it.