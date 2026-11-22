"""Ed25519 (RFC 8032) in pure Python so licence verification needs no extra
packages on the customer's VPS. Verification is the production path; signing
exists for the vendor tool and tests. Not constant-time: never use `sign` with
a secret key on a shared/untrusted machine other than the vendor's own."""
import hashlib

p = 2 ** 255 - 19
q = 2 ** 252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p
I = pow(2, (p - 1) // 4, p)


def _inv(x):
    return pow(x, p - 2, p)


def _xrecover(y):
    xx = (y * y - 1) * _inv(d * y * y + 1)
    x = pow(xx, (p + 3) // 8, p)
    if (x * x - xx) % p != 0:
        x = x * I % p
    if x % 2 != 0:
        x = p - x
    return x


By = 4 * _inv(5) % p
Bx = _xrecover(By)
B = (Bx % p, By % p, 1, (Bx * By) % p)
IDENT = (0, 1, 1, 0)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    Bv = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, G, H = Bv - A, D - C, D + C, Bv + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)


def _mul(s, P):
    Q = IDENT
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0


def _compress(P):
    zi = _inv(P[2])
    x, y = P[0] * zi % p, P[1] * zi % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= p:
        return None
    x = _xrecover(y)
    if (x & 1) != sign:
        x = p - x
    P = (x, y, 1, x * y % p)
    # on-curve check
    if (-x * x + y * y - 1 - d * x * x * y * y) % p != 0:
        return None
    return P


def _h(m):
    return int.from_bytes(hashlib.sha512(m).digest(), "little")


def _secret_expand(sk):
    h = hashlib.sha512(sk).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(sk):
    a, _ = _secret_expand(sk)
    return _compress(_mul(a, B))


def sign(sk, msg):
    a, prefix = _secret_expand(sk)
    A = _compress(_mul(a, B))
    r = _h(prefix + msg) % q
    R = _compress(_mul(r, B))
    k = _h(R + A + msg) % q
    S = (r + k * a) % q
    return R + int.to_bytes(S, 32, "little")


def verify(pk, msg, sig):
    """True only for a valid signature. Never raises on malformed input."""
    try:
        if len(pk) != 32 or len(sig) != 64:
            return False
        A = _decompress(pk)
        R = _decompress(sig[:32])
        S = int.from_bytes(sig[32:], "little")
        if A is None or R is None or S >= q:
            return False
        k = _h(sig[:32] + pk + msg) % q
        return _equal(_mul(S, B), _add(R, _mul(k, A)))
    except (ValueError, TypeError):
        return False
