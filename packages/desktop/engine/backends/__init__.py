"""Storage backend plugins for OpenAdapt Desktop.

Each backend implements the StorageBackend protocol defined in protocol.py.
Backends are loaded conditionally based on build-time feature flags and
runtime configuration.

Available backends:
    s3          S3-compatible (AWS S3, Cloudflare R2, MinIO)
    huggingface HuggingFace Hub (public/private datasets)
    wormhole    Magic Wormhole (P2P ephemeral transfer)
    federated   Federated learning gradient upload (Flower)
"""
