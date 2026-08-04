from __future__ import annotations


SERVICE_NAME_PREFIX = "SMSIArchiveClient/R2"
ACCESS_KEY_USER = "access-key-id"
SECRET_KEY_USER = "secret-access-key"


class CredentialStore:
    """Store R2 credentials in the current user's credential vault."""

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError("缺少 keyring，请先安装客户端依赖") from exc
        return keyring

    @staticmethod
    def _service_name(profile_id: str) -> str:
        value = profile_id.strip()
        if not value or any(char in value for char in "/\\"):
            raise ValueError("配置 ID 无效，无法访问 R2 凭据")
        return f"{SERVICE_NAME_PREFIX}/{value}"

    def get_r2(self, profile_id: str) -> tuple[str, str]:
        keyring = self._keyring()
        service = self._service_name(profile_id)
        return (
            keyring.get_password(service, ACCESS_KEY_USER) or "",
            keyring.get_password(service, SECRET_KEY_USER) or "",
        )

    def set_r2(
        self, profile_id: str, access_key_id: str, secret_access_key: str
    ) -> None:
        access_key_id = access_key_id.strip()
        secret_access_key = secret_access_key.strip()
        if not access_key_id or not secret_access_key:
            raise ValueError("R2 Access Key 和 Secret 都不能为空")
        keyring = self._keyring()
        service = self._service_name(profile_id)
        keyring.set_password(service, ACCESS_KEY_USER, access_key_id)
        keyring.set_password(service, SECRET_KEY_USER, secret_access_key)

    def clear_r2(self, profile_id: str) -> None:
        keyring = self._keyring()
        service = self._service_name(profile_id)
        for username in (ACCESS_KEY_USER, SECRET_KEY_USER):
            try:
                keyring.delete_password(service, username)
            except keyring.errors.PasswordDeleteError:
                pass
