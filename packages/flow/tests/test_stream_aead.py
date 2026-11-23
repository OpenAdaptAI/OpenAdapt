"""The sealed-set v3 payload is Tink's AES256_GCM_HKDF_1MB wire format.

Checked both ways against Tink itself (a dev dependency) at the segment
boundaries, and for truncation and tampering.
"""

from __future__ import annotations

import io
import os

import pytest

from openadapt_flow import stream_aead

SIZES = (0, 1, 1048535, 1048536, 1048537, 2 * 1048576 + 5)
AAD = b"openadapt-flow/sealed-set/file/run/steps/step_1_after.png"


def _tink(ikm: bytes):
    tink = pytest.importorskip("tink")
    from tink import cleartext_keyset_handle, streaming_aead
    from tink.proto import aes_gcm_hkdf_streaming_pb2 as pb
    from tink.proto import common_pb2, tink_pb2

    streaming_aead.register()
    key = pb.AesGcmHkdfStreamingKey(
        version=0,
        key_value=ikm,
        params=pb.AesGcmHkdfStreamingParams(
            ciphertext_segment_size=1 << 20,
            derived_key_size=32,
            hkdf_hash_type=common_pb2.SHA256,
        ),
    )
    keyset = tink_pb2.Keyset(
        primary_key_id=1,
        key=[
            tink_pb2.Keyset.Key(
                key_data=tink_pb2.KeyData(
                    type_url="type.googleapis.com/google.crypto.tink.AesGcmHkdfStreamingKey",
                    value=key.SerializeToString(),
                    key_material_type=tink_pb2.KeyData.SYMMETRIC,
                ),
                status=tink_pb2.ENABLED,
                key_id=1,
                output_prefix_type=tink_pb2.RAW,
            )
        ],
    )
    assert tink
    return cleartext_keyset_handle.from_keyset(keyset).primitive(
        streaming_aead.StreamingAead
    )


@pytest.mark.parametrize("size", SIZES)
def test_tink_reads_ours_and_we_read_tinks(size: int) -> None:
    ikm = os.urandom(32)
    primitive = _tink(ikm)
    plaintext = os.urandom(size)
    ours = stream_aead.encrypt(ikm, plaintext, AAD)
    with primitive.new_decrypting_stream(io.BytesIO(ours), AAD) as stream:
        assert stream.read() == plaintext

    class Keep(io.BytesIO):
        def close(self) -> None:
            self.kept = self.getvalue()
            super().close()

    sink = Keep()
    with primitive.new_encrypting_stream(sink, AAD) as stream:
        stream.write(plaintext)
    assert stream_aead.decrypt(ikm, sink.kept, AAD) == plaintext
    assert len(sink.kept) == len(ours)


def test_truncation_tampering_and_another_path_fail() -> None:
    ikm = os.urandom(32)
    data = stream_aead.encrypt(ikm, os.urandom(2 * 1048576), AAD)
    with pytest.raises(stream_aead.StreamError):
        stream_aead.decrypt(ikm, data[:1048576], AAD)  # a crash mid-stream
    flipped = bytearray(data)
    flipped[100] ^= 1
    with pytest.raises(stream_aead.StreamError):
        stream_aead.decrypt(ikm, bytes(flipped), AAD)
    with pytest.raises(stream_aead.StreamError):
        stream_aead.decrypt(ikm, data, b"another/path")
    with pytest.raises(stream_aead.StreamError):
        stream_aead.decrypt(os.urandom(32), data, AAD)
