"""Storage backend plugins for OpenAdapt Desktop.

Each backend implements the StorageBackend protocol defined in protocol.py.
Backends are loaded conditionally based on build-time feature flags and
runtime configuration.

Available backends:
    hosted_ingest  Hosted control plane (POST /api/ingest, bearer token)
    s3             S3-compatible (AWS S3, Cloudflare R2, MinIO) -- optional BYOC storage
"""
