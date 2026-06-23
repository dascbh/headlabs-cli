"""Tests for _ephemeral_credentials (Option B credential derivation)."""

from headlabs.client import _ephemeral_credentials


class _Frozen:
    def __init__(self, ak, sk, token):
        self.access_key, self.secret_key, self.token = ak, sk, token


class _Creds:
    def __init__(self, frozen):
        self._f = frozen

    def get_frozen_credentials(self):
        return self._f


class _STS:
    def __init__(self, resp=None, fail=False):
        self._resp, self._fail = resp, fail

    def get_session_token(self, DurationSeconds=3600):
        if self._fail:
            raise RuntimeError("MFA required")
        return self._resp


class _Session:
    def __init__(self, creds, sts=None):
        self._creds, self._sts = creds, sts

    def get_credentials(self):
        return self._creds

    def client(self, name):
        return self._sts


def test_forwards_existing_temporary_credentials():
    # SSO / assumed-role: already carries a session token -> forward as-is.
    s = _Session(_Creds(_Frozen("ASIA123", "sek", "tok")))
    assert _ephemeral_credentials(s) == {
        "aws_access_key_id": "ASIA123",
        "aws_secret_access_key": "sek",
        "aws_session_token": "tok",
    }


def test_static_keys_minted_via_sts_session_token():
    # Static IAM keys (no token) -> exchange for a short-lived session token.
    sts = _STS(resp={"Credentials": {
        "AccessKeyId": "ASIA2", "SecretAccessKey": "s2", "SessionToken": "t2"}})
    s = _Session(_Creds(_Frozen("AKIA", "longsecret", None)), sts)
    c = _ephemeral_credentials(s)
    assert c == {"aws_access_key_id": "ASIA2",
                 "aws_secret_access_key": "s2",
                 "aws_session_token": "t2"}


def test_static_keys_fallback_when_get_session_token_fails():
    # If STS GetSessionToken fails (e.g. MFA), forward static keys (no token).
    s = _Session(_Creds(_Frozen("AKIA", "longsecret", None)), _STS(fail=True))
    assert _ephemeral_credentials(s) == {
        "aws_access_key_id": "AKIA", "aws_secret_access_key": "longsecret"}


def test_none_when_no_credentials():
    assert _ephemeral_credentials(_Session(None)) is None
