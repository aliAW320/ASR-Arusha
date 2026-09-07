# The actual word/segment-to-speaker merge algorithm lives in the
# dependency-free `alignment` package (src/alignment/merge.py) so
# app/services/processing.py -- imported by every service's container, not
# just this one -- can call it without pulling this worker's own code into
# theirs. Re-exported here so existing `from diarization.alignment import
# align_transcript_to_speakers` call sites keep working unchanged.
from alignment.merge import align_transcript_to_speakers  # noqa: F401
