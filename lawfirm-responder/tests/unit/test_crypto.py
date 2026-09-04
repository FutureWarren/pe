import base64

import pytest

from responder.gateway.wecom_crypto import WeComCrypto, WeComCryptoError

TOKEN = "testtoken"
AES_KEY = base64.b64encode(b"x" * 32).decode()[:43]
CORP_ID = "wwtestcorpid"


@pytest.fixture
def crypto() -> WeComCrypto:
    return WeComCrypto(TOKEN, AES_KEY, CORP_ID)


def test_roundtrip(crypto):
    msg = "<xml><Content>你好，判几年？</Content></xml>"
    assert crypto.decrypt(crypto.encrypt(msg)) == msg


def test_signature_verify(crypto):
    encrypt = crypto.encrypt("hello")
    sig = crypto.signature("1700000000", "nonce1", encrypt)
    assert crypto.verify(sig, "1700000000", "nonce1", encrypt)
    assert not crypto.verify(sig, "1700000001", "nonce1", encrypt)


def test_receive_id_mismatch(crypto):
    other = WeComCrypto(TOKEN, AES_KEY, "othercorp")
    encrypt = other.encrypt("hello")
    with pytest.raises(WeComCryptoError):
        crypto.decrypt(encrypt)


def test_bad_key_length():
    with pytest.raises(WeComCryptoError):
        WeComCrypto(TOKEN, "short", CORP_ID)


def test_build_reply(crypto):
    reply = crypto.build_reply("<xml>ok</xml>", "nonce2")
    assert crypto.verify(
        reply["msgsignature"], reply["timestamp"], reply["nonce"], reply["encrypt"]
    )
