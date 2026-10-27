"""AES-128 decryption in pure Python, for one job: reading a browser's cookie store.

Chrome-family browsers encrypt cookie values with AES-128-CBC. Decrypting them
needs a block cipher, and this project's direct-dependency budget is spent
(ADR-0008: 5 of 5). The alternatives are worse than ~100 lines of standard
algorithm: a sixth dependency, or shelling out to ``openssl enc -K <hex>``,
which puts the key in a child process's argv where ``ps`` shows it
(``docs/memory/credential-leak-channels.md``).

**Decrypt only.** There is no encrypt path here; the test suite carries its own
(``tests/unit/_aes_encrypt.py``) so shipping code cannot be mistaken for a
general-purpose cipher. This is a transcription of FIPS-197 for a few hundred
bytes of cookie, not constant-time and not for anything adversarial.
Correctness is pinned by the FIPS-197 Appendix C.1 and NIST SP 800-38A F.2.2
vectors in ``tests/unit/test_aes.py``.

Errors carry no data: a ``ValueError`` here may be raised with a key or a
plaintext in scope, and its message must not be able to repeat either.
"""

from __future__ import annotations

__all__ = ["BLOCK_SIZE", "aes128_cbc_decrypt", "expand_key", "pkcs7_unpad"]

BLOCK_SIZE = 16
_ROUNDS = 10


def _xtime(value: int) -> int:
    """Multiply by x in GF(2^8) modulo x^8 + x^4 + x^3 + x + 1."""
    value <<= 1
    return (value ^ 0x11B) & 0xFF if value & 0x100 else value


def _gmul(a: int, b: int) -> int:
    """Multiply two bytes in GF(2^8)."""
    product = 0
    while b:
        if b & 1:
            product ^= a
        a = _xtime(a)
        b >>= 1
    return product


def _build_sbox() -> tuple[int, ...]:
    """Derive the S-box from its definition instead of typing 256 constants.

    S(x) = affine(x^-1), with x^-1 found by walking the multiplicative group
    (3 is a generator), so a transcription typo is not possible. The FIPS-197
    vector test would catch a wrong table either way.
    """
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ _xtime(p)  # p *= 3
        q ^= q << 1  # q /= 3
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        rotated = lambda v, n: ((v << n) | (v >> (8 - n))) & 0xFF  # noqa: E731
        sbox[p] = q ^ rotated(q, 1) ^ rotated(q, 2) ^ rotated(q, 3) ^ rotated(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return tuple(sbox)


_SBOX = _build_sbox()
_INV_SBOX = tuple(sorted(range(256), key=_SBOX.__getitem__))
_MUL = {n: tuple(_gmul(n, x) for x in range(256)) for n in (9, 11, 13, 14)}


def expand_key(key: bytes) -> list[list[int]]:
    """Expand a 16-byte key into eleven 16-byte round keys.

    :raises ValueError: if ``key`` is not 16 bytes.
    """
    if len(key) != 16:
        raise ValueError("AES-128 needs a 16-byte key.")
    words = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 4 * (_ROUNDS + 1)):
        word = list(words[i - 1])
        if i % 4 == 0:
            word = word[1:] + word[:1]
            word = [_SBOX[b] for b in word]
            word[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([a ^ b for a, b in zip(words[i - 4], word, strict=True)])
    return [sum(words[4 * r : 4 * r + 4], []) for r in range(_ROUNDS + 1)]


def _add_round_key(state: list[int], round_key: list[int]) -> list[int]:
    return [a ^ b for a, b in zip(state, round_key, strict=True)]


def _inv_shift_rows(state: list[int]) -> list[int]:
    # State is column-major: byte i sits at row i % 4, column i // 4.
    return [state[row + 4 * ((col - row) % 4)] for col in range(4) for row in range(4)]


def _inv_mix_columns(state: list[int]) -> list[int]:
    m9, m11, m13, m14 = _MUL[9], _MUL[11], _MUL[13], _MUL[14]
    out: list[int] = []
    for col in range(0, 16, 4):
        a0, a1, a2, a3 = state[col : col + 4]
        out += [
            m14[a0] ^ m11[a1] ^ m13[a2] ^ m9[a3],
            m9[a0] ^ m14[a1] ^ m11[a2] ^ m13[a3],
            m13[a0] ^ m9[a1] ^ m14[a2] ^ m11[a3],
            m11[a0] ^ m13[a1] ^ m9[a2] ^ m14[a3],
        ]
    return out


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = _add_round_key(list(block), round_keys[_ROUNDS])
    for rnd in range(_ROUNDS - 1, 0, -1):
        state = _inv_shift_rows(state)
        state = [_INV_SBOX[b] for b in state]
        state = _add_round_key(state, round_keys[rnd])
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [_INV_SBOX[b] for b in state]
    return bytes(_add_round_key(state, round_keys[0]))


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Decrypt ``data`` with AES-128-CBC. Padding is **not** removed.

    :raises ValueError: if the key or IV is not 16 bytes, or ``data`` is empty
        or not a whole number of blocks. The message never repeats an input.
    """
    if len(iv) != BLOCK_SIZE:
        raise ValueError("AES-CBC needs a 16-byte IV.")
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError("Ciphertext is not a whole number of 16-byte blocks.")
    round_keys = expand_key(key)
    previous = iv
    out = bytearray()
    for start in range(0, len(data), BLOCK_SIZE):
        block = data[start : start + BLOCK_SIZE]
        plain = _decrypt_block(block, round_keys)
        out += bytes(a ^ b for a, b in zip(plain, previous, strict=True))
        previous = block
    return bytes(out)


def pkcs7_unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding.

    :raises ValueError: if the padding is malformed, which is also what a wrong
        key looks like — the message says so without echoing any byte.
    """
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError("Padded data is not a whole number of 16-byte blocks.")
    pad = data[-1]
    if not 1 <= pad <= BLOCK_SIZE or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("Invalid PKCS#7 padding (wrong key?).")
    return data[:-pad]
