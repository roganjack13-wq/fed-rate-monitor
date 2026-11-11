"""wincred.py — 用 ctypes 讀寫 Windows 認證管理員（一般認證），不需要另外安裝 keyring 套件。

存進去的項目可以在「控制台 → 認證管理員 → Windows 認證 → 一般認證」看到與刪除。
非 Windows 系統上所有函式都安靜地回 None／False。
"""
from __future__ import annotations

import ctypes
import sys
from typing import Optional

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


def available() -> bool:
    return sys.platform == "win32"


if available():
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    _adv.CredReadW.restype = wintypes.BOOL
    _adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    _adv.CredWriteW.restype = wintypes.BOOL
    _adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _adv.CredDeleteW.restype = wintypes.BOOL
    _adv.CredFree.argtypes = [ctypes.c_void_p]
    _adv.CredFree.restype = None


def read(target: str) -> Optional[str]:
    """讀一個項目；不存在或不是 Windows 就回 None。"""
    if not available():
        return None
    ptr = ctypes.POINTER(_CREDENTIALW)()
    if not _adv.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        _adv.CredFree(ctypes.cast(ptr, ctypes.c_void_p))


def write(target: str, secret: str, username: str = "fed-rate-monitor") -> None:
    """寫入（已存在就覆蓋）。失敗丟 OSError。"""
    if not available():
        raise OSError("Windows Credential Manager is only available on Windows")
    blob = secret.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIALW()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = username
    if not _adv.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "CredWriteW failed")


def delete(target: str) -> bool:
    if not available():
        return False
    return bool(_adv.CredDeleteW(target, CRED_TYPE_GENERIC, 0))
