"""File storage for uploads: the bytes live here, state holds only a FileRef.

Two backends share one interface:
- LocalFileStore: a folder on disk, for development and tests.
- S3FileStore: the existing S3 bucket, reusing the SigV4 helpers in tools/.

Pick one with KAI_FILE_STORE=local|s3 (default local, under KAI_FILE_ROOT or
./.kai_files), or pass a store per run in config["configurable"]["file_store"].
"""
from __future__ import annotations

import io
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Optional, Protocol

from kai.state import FileKind, FileRef

_KIND_BY_EXT: dict[str, FileKind] = {
    ".csv": "csv",
    ".xlsx": "excel", ".xls": "excel",
    ".pdf": "pdf",
    ".txt": "text", ".md": "text", ".json": "text", ".xml": "text",
    ".yaml": "text", ".yml": "text",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
}


def file_kind(name: str) -> FileKind:
    return _KIND_BY_EXT.get(Path(name).suffix.lower(), "other")


def new_file_ref(name: str, size: int, key_prefix: str = "uploads") -> FileRef:
    file_id = "f_" + uuid.uuid4().hex[:8]
    safe_name = Path(name).name or "upload"
    return FileRef(
        id=file_id,
        name=safe_name,
        mime=mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
        kind=file_kind(safe_name),
        size=size,
        key=f"{key_prefix}/{file_id}/{safe_name}",
    )


class FileStore(Protocol):
    def put(self, name: str, data: bytes) -> FileRef: ...
    def read_bytes(self, ref: FileRef) -> bytes: ...
    def locator(self, ref: FileRef) -> str:
        """A path or URL that file-path based tools (e.g. the Excel tools) can open."""
        ...


def read_text(store: FileStore, ref: FileRef, max_chars: int = 2_000_000) -> str:
    """Text of a CSV, text or PDF file. Raises ValueError for other kinds."""
    data = store.read_bytes(ref)
    if ref["kind"] in ("csv", "text"):
        return data.decode("utf-8-sig", errors="replace")[:max_chars]
    if ref["kind"] == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
        return "\n\n".join(pages)[:max_chars]
    raise ValueError(f"{ref['name']} is a {ref['kind']} file and has no text content.")


class LocalFileStore:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root or os.environ.get("KAI_FILE_ROOT") or ".kai_files").resolve()

    def _path(self, ref: FileRef) -> Path:
        path = (self.root / ref["key"]).resolve()
        if self.root not in path.parents:
            raise ValueError("File key points outside the store root.")
        return path

    def put(self, name: str, data: bytes) -> FileRef:
        ref = new_file_ref(name, len(data))
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ref

    def read_bytes(self, ref: FileRef) -> bytes:
        return self._path(ref).read_bytes()

    def locator(self, ref: FileRef) -> str:
        return str(self._path(ref))


class S3FileStore:
    """Uses the SigV4 helpers already in tools/ (credentials from env only)."""

    def __init__(self, prefix: str = "uploads"):
        self.prefix = prefix

    @staticmethod
    def _env():
        from tools.file_upload_s3_tool import _s3_env

        access_key, secret_key, bucket, region = _s3_env()
        if not (access_key and secret_key and bucket):
            raise RuntimeError("S3 is not configured: set AWS_ACCESS_KEY_ID, "
                               "AWS_SECRET_ACCESS_KEY and AWS_S3_BUCKET.")
        return access_key, secret_key, bucket, region

    def put(self, name: str, data: bytes) -> FileRef:
        from tools.file_upload_s3_tool import _s3_put

        ref = new_file_ref(name, len(data), self.prefix)
        _s3_put(ref["key"], data, ref["mime"], *self._env())
        return ref

    def read_bytes(self, ref: FileRef) -> bytes:
        import urllib.parse

        from tools.chart_s3_tool import _s3_request

        return _s3_request("GET", urllib.parse.quote(ref["key"], safe="/"), {}, *self._env())

    def locator(self, ref: FileRef) -> str:
        from tools.file_upload_s3_tool import _presigned_get

        return _presigned_get(ref["key"], *self._env(), expires=3600)


_default: Optional[FileStore] = None


def default_store() -> FileStore:
    global _default
    if _default is None:
        backend = os.environ.get("KAI_FILE_STORE", "local").lower()
        _default = S3FileStore() if backend == "s3" else LocalFileStore()
    return _default


def store_from_config(config: Optional[dict]) -> FileStore:
    store = ((config or {}).get("configurable") or {}).get("file_store")
    return store or default_store()
