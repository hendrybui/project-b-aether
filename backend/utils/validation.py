from pathlib import Path

from domain.enums import SourceType
from domain.models import CreateJobRequest


class ValidationError(Exception):
    pass


VALID_STEMS = {"vocals", "drums", "bass", "guitar", "piano", "other"}
VALID_UPLOAD_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".mp4", ".mov", ".mkv"}


def validate_source_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def validate_upload_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in VALID_UPLOAD_EXTENSIONS


def validate_create_job_request(payload: CreateJobRequest) -> None:
    if not payload.stems:
        raise ValidationError("At least one stem must be selected")

    invalid_stems = [stem for stem in payload.stems if stem not in VALID_STEMS]
    if invalid_stems:
        raise ValidationError(f"Invalid stems requested: {', '.join(invalid_stems)}")

    if payload.source_type in {SourceType.youtube, SourceType.media_url}:
        if not payload.url or not validate_source_url(payload.url):
            raise ValidationError("A valid http/https URL is required for URL-based jobs")

    if payload.source_type is SourceType.upload:
        if not payload.filename:
            raise ValidationError("An uploaded filename is required for upload jobs")
        if not validate_upload_filename(payload.filename):
            raise ValidationError("Unsupported upload file type")
