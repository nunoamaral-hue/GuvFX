"""Pure-standard-library Ed25519 signature VERIFICATION (RFC 8032, Ed25519 / edwards25519).

This module exists so that TIM can verify an operational producer signature with **no third-party
dependency** (the TIM research/governance lanes are stdlib-only; ``cryptography`` is not importable
there). It implements **verification only** — there is deliberately NO key generation and NO signing.
That is a hard boundary, not an omission: under ADR-0070 the GuvFX producer is the signer and TIM is a
read-only consumer that may never hold or exercise operational signing authority. A signing primitive
does not belong in this repository.

``ed25519_verify`` fails closed: any structural problem (wrong length, non-canonical scalar, point not
on the curve, arithmetic failure) returns ``False`` rather than raising. It takes and returns bytes/bools
only; it never logs, never serialises inputs, and never carries key material into an exception message.

The implementation follows the RFC 8032 reference and verifies strictly: it rejects a non-canonical
``S >= L`` (malleability guard), non-canonical point encodings (``y >= p`` or a set sign bit on ``x == 0``),
and small-order ``A`` or ``R`` (so a crafted signature against a degenerate/low-order key cannot be
accepted). It is validated against RFC 8032 test vector 1 and, in the test suite and an offline
differential fuzz, against an independent implementation — accepting every genuine signature of a
full-order key and rejecting every tamper, wrong key, wrong length, non-canonical scalar/point and
small-order case.
"""
from __future__ import annotations

import hashlib

_P = 2 ** 255 - 19
# group order L (RFC 8032 section 5.1)
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = _xrecover(_BY)
# Base point in extended (X, Y, Z, T) coordinates.
_B = (_BX % _P, _BY % _P, 1, (_BX * _BY) % _P)


def _edwards_add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (t1 * 2 * _D * t2) % _P
    dd = (z1 * 2 * z2) % _P
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalarmult(p, e: int):
    if e == 0:
        return (0, 1, 1, 0)
    q = _scalarmult(p, e // 2)
    q = _edwards_add(q, q)
    if e & 1:
        q = _edwards_add(q, p)
    return q


def _encodepoint(p) -> bytes:
    x, y, z, _t = p
    zi = pow(z, _P - 2, _P)
    x = (x * zi) % _P
    y = (y * zi) % _P
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))


def _bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def _isoncurve(p) -> bool:
    x, y, z, t = p
    return (
        z % _P != 0
        and (x * y) % _P == (z * t) % _P
        and (y * y - x * x - z * z - _D * t * t) % _P == 0
    )


def _decodepoint(s: bytes):
    y = sum(2 ** i * _bit(s, i) for i in range(255))
    if y >= _P:  # non-canonical y encoding (y must be the least residue mod p)
        raise ValueError("non-canonical point encoding")
    x = _xrecover(y)
    if x == 0 and _bit(s, 255) == 1:  # non-canonical sign bit on x == 0 (there is no -0)
        raise ValueError("non-canonical sign bit")
    if x & 1 != _bit(s, 255):
        x = _P - x
    p = (x % _P, y % _P, 1, (x * y) % _P)
    if not _isoncurve(p):
        raise ValueError("point not on curve")
    return p


_IDENTITY_ENC = _encodepoint((0, 1, 1, 0))


def _is_small_order(point) -> bool:
    """True iff ``point`` has order dividing 8 (the small subgroup / identity): ``[8]·point == identity``.
    Genuine Ed25519 verification keys and signature R values are full (prime) order, never small order."""
    return _encodepoint(_scalarmult(point, 8)) == _IDENTITY_ENC


def is_acceptable_public_key(public_key: bytes) -> bool:
    """True iff ``public_key`` is a canonical, on-curve, NON-small-order edwards25519 point suitable as an
    Ed25519 verification key. Used to refuse pinning a degenerate / low-order key (with which crafted
    signatures could otherwise be contrived). Fails closed (returns False) on any structural problem."""
    try:
        if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != 32:
            return False
        return not _is_small_order(_decodepoint(bytes(public_key)))
    except Exception:
        return False


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Return True iff ``signature`` is a valid Ed25519 signature of ``message`` under ``public_key``.

    Fails closed: returns False on any structural error (wrong lengths, non-canonical scalar, point not
    on the curve, or any arithmetic exception). Never raises, never logs, never echoes inputs.
    """
    try:
        if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != 32:
            return False
        if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
            return False
        public_key = bytes(public_key)
        signature = bytes(signature)
        r = signature[:32]
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:  # non-canonical S — reject (malleability guard)
            return False
        a_point = _decodepoint(public_key)
        r_point = _decodepoint(r)
        if _is_small_order(a_point) or _is_small_order(r_point):
            return False  # reject degenerate / small-order A or R (strict, fail-closed)
        h = int.from_bytes(hashlib.sha512(r + public_key + message).digest(), "little") % _L
        lhs = _scalarmult(_B, s)
        rhs = _edwards_add(r_point, _scalarmult(a_point, h))
        return _encodepoint(lhs) == _encodepoint(rhs)
    except Exception:
        return False
