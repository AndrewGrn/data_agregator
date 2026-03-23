from app.security import hash_password, verify_password


def test_password_hashing_roundtrip():
    password = "super-secret-123"
    digest = hash_password(password)

    assert digest != password
    assert verify_password(password, digest)
    assert not verify_password("wrong", digest)
