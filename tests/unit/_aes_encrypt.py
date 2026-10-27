"""AES-128-CBC *encrypt*, for tests only.

Shipping code decrypts and never encrypts (``auth/_aes.py``). Fixture browser
profiles need real ciphertext, so the encrypt half lives here, built from the
same S-box and key schedule — which means a bug in those would cancel out; the
FIPS-197 and SP 800-38A vectors in ``test_aes.py`` are what pin them.
"""

from __future__ import annotations

from fantasy_sports.auth import _aes


def _encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = [a ^ b for a, b in zip(block, round_keys[0], strict=True)]
    for rnd in range(1, 11):
        state = [_aes._SBOX[b] for b in state]
        # ShiftRows (column-major state).
        state = [state[row + 4 * ((col + row) % 4)] for col in range(4) for row in range(4)]
        if rnd != 10:
            mixed: list[int] = []
            for col in range(0, 16, 4):
                a0, a1, a2, a3 = state[col : col + 4]
                mixed += [
                    _aes._gmul(2, a0) ^ _aes._gmul(3, a1) ^ a2 ^ a3,
                    a0 ^ _aes._gmul(2, a1) ^ _aes._gmul(3, a2) ^ a3,
                    a0 ^ a1 ^ _aes._gmul(2, a2) ^ _aes._gmul(3, a3),
                    _aes._gmul(3, a0) ^ a1 ^ a2 ^ _aes._gmul(2, a3),
                ]
            state = mixed
        state = [a ^ b for a, b in zip(state, round_keys[rnd], strict=True)]
    return bytes(state)


def aes128_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Encrypt whole blocks (no padding added)."""
    assert len(data) % 16 == 0
    round_keys = _aes.expand_key(key)
    previous = iv
    out = bytearray()
    for start in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[start : start + 16], previous, strict=True))
        previous = _encrypt_block(block, round_keys)
        out += previous
    return bytes(out)


def pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad
