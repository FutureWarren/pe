"""企业微信回调消息加解密（WXBizMsgCrypt 标准实现）。

协议：SHA1 签名校验 + AES-256-CBC（PKCS7 填充），参考企微官方回调协议文档。
"""

import base64
import hashlib
import socket
import struct
import time

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes


class WeComCryptoError(Exception):
    pass


def _pkcs7_pad(data: bytes, block_size: int = 32) -> bytes:
    pad_len = block_size - len(data) % block_size
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes) -> bytes:
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 32:
        pad_len = 0
    return data[:-pad_len] if pad_len else data


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        self.token = token
        self.receive_id = receive_id
        if len(encoding_aes_key) != 43:
            raise WeComCryptoError("EncodingAESKey 长度必须为 43")
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    # ------------------------------------------------------------ 签名
    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        items = sorted([self.token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(items).encode()).hexdigest()

    def verify(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        return self.signature(timestamp, nonce, encrypt) == msg_signature

    # ------------------------------------------------------------ 解密
    def decrypt(self, encrypt: str) -> str:
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        plain = _pkcs7_unpad(cipher.decrypt(base64.b64decode(encrypt)))
        content = plain[16:]  # 去掉 16 字节随机串
        msg_len = socket.ntohl(struct.unpack("I", content[:4])[0])
        msg = content[4 : 4 + msg_len].decode("utf-8")
        receive_id = content[4 + msg_len :].decode("utf-8")
        if self.receive_id and receive_id != self.receive_id:
            raise WeComCryptoError("receive_id 校验失败")
        return msg

    # ------------------------------------------------------------ 加密
    def encrypt(self, msg: str) -> str:
        msg_bytes = msg.encode("utf-8")
        payload = (
            get_random_bytes(16)
            + struct.pack("I", socket.htonl(len(msg_bytes)))
            + msg_bytes
            + self.receive_id.encode("utf-8")
        )
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        return base64.b64encode(cipher.encrypt(_pkcs7_pad(payload))).decode()

    def build_reply(self, msg: str, nonce: str, timestamp: str | None = None) -> dict:
        timestamp = timestamp or str(int(time.time()))
        encrypt = self.encrypt(msg)
        return {
            "encrypt": encrypt,
            "msgsignature": self.signature(timestamp, nonce, encrypt),
            "timestamp": timestamp,
            "nonce": nonce,
        }
