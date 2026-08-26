from enum import Enum


class SourceType(str, Enum):
    upload = "upload"
    youtube = "youtube"
    media_url = "media_url"


class JobStatus(str, Enum):
    created = "created"
    validating_input = "validating_input"
    ingesting_source = "ingesting_source"
    transcoding = "transcoding"
    separating = "separating"
    postprocessing = "postprocessing"
    analyzing = "analyzing"
    packaging = "packaging"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class StemName(str, Enum):
    vocals = "vocals"
    drums = "drums"
    bass = "bass"
    guitar = "guitar"
    piano = "piano"
    other = "other"
    original = "original"
    mix = "mix"
