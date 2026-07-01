"""Accès au stockage objet (MinIO / S3) — corpus cegid-sources."""
from __future__ import annotations

from functools import lru_cache

from .config import settings


@lru_cache(maxsize=1)
def client():
    if not settings.s3_configured:
        raise RuntimeError("Stockage objet (S3/MinIO) non configuré")
    from minio import Minio
    return Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )


def list_objects(prefix: str = "") -> list[dict]:
    objs = client().list_objects(settings.s3_bucket, prefix=prefix, recursive=True)
    out = []
    for o in objs:
        if o.is_dir:
            continue
        out.append({"key": o.object_name, "size": o.size, "last_modified": str(o.last_modified)})
    return out


def get_bytes(key: str) -> bytes:
    resp = client().get_object(settings.s3_bucket, key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    import io
    client().put_object(
        settings.s3_bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def bucket_exists() -> bool:
    return client().bucket_exists(settings.s3_bucket)
