"""The pure-Python AES-128 decrypt, pinned to the published test vectors."""

from __future__ import annotations

import pytest
from _aes_encrypt import aes128_cbc_encrypt, pkcs7_pad

from fantasy_sports.auth import _aes

# FIPS-197 Appendix C.1 (AES-128).
FIPS_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_PLAIN = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CIPHER = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")

# NIST SP 800-38A F.2.2 (CBC-AES128.Decrypt).
CBC_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
CBC_IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
CBC_CIPHER = bytes.fromhex(
    "7649abac8119b246cee98e9b12e9197d"
    "5086cb9b507219ee95db113a917678b2"
    "73bed6b8e3c1743b7116e69e22229516"
    "3ff1caa1681fac09120eca307586e1a7"
)
CBC_PLAIN = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)


def test_fips_197_c1_block_decrypts():
    # CBC with a zero IV over one block is the bare block cipher.
    assert _aes.aes128_cbc_decrypt(FIPS_KEY, bytes(16), FIPS_CIPHER) == FIPS_PLAIN


def test_sp_800_38a_f22_cbc_decrypts_four_blocks():
    assert _aes.aes128_cbc_decrypt(CBC_KEY, CBC_IV, CBC_CIPHER) == CBC_PLAIN


def test_sbox_matches_known_entries():
    assert _aes._SBOX[0x00] == 0x63
    assert _aes._SBOX[0x53] == 0xED
    assert _aes._INV_SBOX[0x63] == 0x00


def test_the_test_only_encrypt_agrees_with_the_published_vectors():
    assert aes128_cbc_encrypt(FIPS_KEY, bytes(16), FIPS_PLAIN) == FIPS_CIPHER
    assert aes128_cbc_encrypt(CBC_KEY, CBC_IV, CBC_PLAIN) == CBC_CIPHER


def test_round_trip_with_padding():
    message = b"espn_s2 value with %2B encoding" * 3
    cipher = aes128_cbc_encrypt(CBC_KEY, b" " * 16, pkcs7_pad(message))
    assert _aes.pkcs7_unpad(_aes.aes128_cbc_decrypt(CBC_KEY, b" " * 16, cipher)) == message


@pytest.mark.parametrize(
    ("key", "iv", "data"),
    [
        (b"short", bytes(16), bytes(16)),
        (FIPS_KEY, b"short", bytes(16)),
        (FIPS_KEY, bytes(16), b""),
        (FIPS_KEY, bytes(16), bytes(17)),
    ],
)
def test_bad_shapes_raise_without_echoing_inputs(key: bytes, iv: bytes, data: bytes):
    with pytest.raises(ValueError, match="AES|Ciphertext") as caught:
        _aes.aes128_cbc_decrypt(key, iv, data)
    assert repr(key) not in str(caught.value)


@pytest.mark.parametrize(
    "data",
    [b"", bytes(15), bytes(16), b"a" * 15 + b"\x11", b"a" * 14 + b"\x01\x02"],
)
def test_bad_padding_is_rejected(data: bytes):
    with pytest.raises(ValueError, match="PKCS#7|blocks"):
        _aes.pkcs7_unpad(data)


def test_full_block_of_padding_is_stripped():
    assert _aes.pkcs7_unpad(b"\x10" * 16) == b""
