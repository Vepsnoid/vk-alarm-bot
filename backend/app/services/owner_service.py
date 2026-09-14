"""Helpers for resolving stream ownership.

Every stream must belong to a real account, so the application never shows a
stream without an owner.
"""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import User


async def resolve_default_owner_id(db: AsyncSession, prefer: Optional[int] = None) -> Optional[int]:
    """Return the user id streams without a valid owner should be assigned to.

    ``prefer`` is used when it points to an existing account (e.g. the admin who
    is acting right now). Otherwise the first administrator is picked, and as a
    last resort the very first account in the database.
    """
    if prefer:
        user = await db.get(User, prefer)
        if user:
            return user.id
    result = await db.execute(select(User.id).where(User.role == "admin").order_by(User.id))
    admin_id = result.scalars().first()
    if admin_id:
        return admin_id
    result = await db.execute(select(User.id).order_by(User.id))
    return result.scalars().first()
