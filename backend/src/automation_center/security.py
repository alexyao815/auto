"""固定账号认证、服务端 Session、CSRF 和敏感配置加解密。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Credential, SessionRecord, utcnow


password_hasher = PasswordHasher()


def sha256_text(value: str) -> str:
    """生成不可逆文本摘要，用于 Token 和 CSRF 的数据库表示。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """使用 Argon2id 创建带随机盐的密码 Hash。"""

    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """验证密码，并把格式损坏等异常统一视为认证失败。"""

    try:
        return password_hasher.verify(password_hash, password)
    except Exception:
        return False


def ensure_initial_credential(session: Session, settings: Settings) -> None:
    """仅在空库中初始化共享账号，避免环境变量覆盖已有密码。"""

    if session.scalar(select(Credential).limit(1)) is not None:
        return
    session.add(Credential(username=settings.initial_username, password_hash=hash_password(settings.initial_password)))
    session.commit()


def reset_credential(session: Session, username: str, password: str) -> None:
    """重置共享账号并删除全部 Session，使旧 Cookie 立即失效。"""

    credential = session.scalar(select(Credential).limit(1))
    if credential is None:
        session.add(Credential(username=username, password_hash=hash_password(password)))
    else:
        credential.username = username
        credential.password_hash = hash_password(password)
        credential.updated_at = utcnow()
    session.query(SessionRecord).delete()
    session.commit()


def create_login_session(session: Session, settings: Settings, source_ip: str) -> tuple[str, str, SessionRecord]:
    """创建随机 Token/CSRF，并仅持久化其 Hash 与 8/24 小时过期点。"""

    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    now = utcnow()
    record = SessionRecord(
        id=str(uuid.uuid4()),
        token_hash=sha256_text(token),
        csrf_hash=sha256_text(csrf),
        source_ip=source_ip,
        idle_expires_at=now + timedelta(seconds=settings.session_idle_seconds),
        absolute_expires_at=now + timedelta(seconds=settings.session_absolute_seconds),
    )
    session.add(record)
    session.commit()
    return token, csrf, record


def encrypt_secret(settings: Settings, value: str) -> str:
    """用应用密钥派生的 Fernet Key 加密敏感设置。"""

    return Fernet(settings.encryption_key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(settings: Settings, value: str) -> str:
    """解密数据库中的敏感设置；明文只存在于进程内存。"""

    return Fernet(settings.encryption_key).decrypt(value.encode("ascii")).decode("utf-8")


def build_auth_dependencies(settings: Settings, get_session):
    """构造共享的 Session 与 CSRF FastAPI 依赖。"""

    def require_session(request: Request, session: Session = Depends(get_session)) -> SessionRecord:
        """验证 Cookie、清理过期记录，并在绝对期限内滑动空闲期限。"""

        raw_token = request.cookies.get(settings.cookie_name)
        if not raw_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或会话已失效")
        record = session.scalar(select(SessionRecord).where(SessionRecord.token_hash == sha256_text(raw_token)))
        now = utcnow()
        if record is None or record.idle_expires_at <= now or record.absolute_expires_at <= now:
            if record is not None:
                session.delete(record)
                session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或会话已失效")
        record.last_seen_at = now
        # 活跃访问只能延长 idle 期限，永远不能突破 absolute_expires_at。
        record.idle_expires_at = min(
            now + timedelta(seconds=settings.session_idle_seconds),
            record.absolute_expires_at,
        )
        session.commit()
        request.state.session_record = record
        return record

    def require_csrf(
        request: Request,
        record: SessionRecord = Depends(require_session),
    ) -> SessionRecord:
        """使用常量时间比较验证写请求携带的 CSRF Token。"""

        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or not hmac.compare_digest(record.csrf_hash, sha256_text(csrf)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
        return record

    return require_session, require_csrf
