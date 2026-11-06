import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select

from kb.config import settings
from kb.db import Employee, Folder, Grant, LoginSession

ROLES = {'owner', 'delegate', 'maker', 'checker', 'reader'}
def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def password_matches(password: str, stored: str) -> bool:
    salt, expected = stored.split(':', 1)
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(expected, actual)


def issue_session(db, employee_id: str) -> str:
    token = secrets.token_urlsafe(48)
    db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), employee_id=employee_id,
                        expires_at=(datetime.now(timezone.utc) + timedelta(hours=settings().session_ttl_hours)).isoformat()))
    return token


def authenticate(db, token: str | None) -> Employee:
    if not token or len(token) > 200:
        raise HTTPException(401, 'Please sign in.')
    session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
    if not session or session.expires_at <= datetime.now(timezone.utc).isoformat():
        raise HTTPException(401, 'Session expired. Please sign in.')
    employee = db.get(Employee, session.employee_id)
    if not employee or not employee.active:
        raise HTTPException(401, 'Account unavailable.')
    return employee


def folder_roles(db, employee_id: str, folder_id: str) -> set[str]:
    # Owner/delegate privileges inherit; ordinary grants are exact-folder only.
    folder = db.get(Folder, folder_id)
    if not folder:
        return set()
    roles = set(db.scalars(select(Grant.role).where(Grant.employee_id == employee_id, Grant.folder_id == folder_id)))
    visited = {folder_id}
    while folder.parent_id and folder.parent_id not in visited:
        folder = db.get(Folder, folder.parent_id)
        if not folder:
            break
        visited.add(folder.id)
        roles |= set(db.scalars(select(Grant.role).where(Grant.employee_id == employee_id, Grant.folder_id == folder.id, Grant.role.in_(['owner', 'delegate']))))
    return roles


def permitted_folders(db, employee_id: str) -> list[str]:
    return [folder.id for folder in db.scalars(select(Folder)) if folder_roles(db, employee_id, folder.id)]


def require_role(db, employee_id: str, folder_id: str, roles: set[str]):
    if not folder_roles(db, employee_id, folder_id).intersection(roles):
        raise HTTPException(403, 'Folder permission denied.')

