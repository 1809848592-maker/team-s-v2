from app.security import new_token, password_hash, token_hash, verify_password


def test_password_hash_is_salted_and_rejects_wrong_password():
    password = "test-only-password"
    first = password_hash(password)
    second = password_hash(password)

    assert first != second
    assert password not in first
    assert verify_password(password, first)
    assert verify_password(password, second)
    assert not verify_password("wrong-password", first)


def test_invalid_stored_password_hash_is_rejected():
    for stored in ("", "plaintext", "other$00$00", "scrypt$not-hex$00"):
        assert not verify_password("test-only-password", stored)


def test_session_tokens_are_random_and_stored_as_hashes():
    first, second = new_token(), new_token()

    assert first != second
    assert token_hash(first) == token_hash(first)
    assert token_hash(first) != token_hash(second)
    assert len(token_hash(first)) == 64
    assert token_hash(first) != first
