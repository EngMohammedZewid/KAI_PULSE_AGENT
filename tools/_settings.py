"""Shared checks for settings the tools read from the environment."""
from __future__ import annotations


def require_bucket(bucket: str) -> str:
    """The S3 bucket from AWS_S3_BUCKET. There is deliberately no default: files
    must never land in an old or shared bucket because a setting was missed."""
    bucket = (bucket or "").strip()
    if not bucket:
        raise RuntimeError("AWS_S3_BUCKET is not set; add the bucket name to .env")
    return bucket
