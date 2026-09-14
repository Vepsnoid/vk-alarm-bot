from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, or_, func
from pydantic import BaseModel
from datetime import datetime

from app.models.database import get_db
from app.models.models import Notification, Monitor
from app.routers.auth import get_current_user
from app.core.redaction import redact_sensitive_data

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _accessible_notification_filter(user):
    """Filter for notifications the user may see."""
    if user.get("role") == "admin":
        return None
    user_id = user.get("id")
    accessible_monitor_ids = select(Monitor.id).where(
        Monitor.owner_id == user_id
    )
    return or_(
        Notification.monitor_id.is_(None),
        Notification.monitor_id.in_(accessible_monitor_ids),
    )


async def _notification_accessible(db: AsyncSession, user, notification) -> bool:
    if user.get("role") == "admin" or notification.monitor_id is None:
        return True
    user_id = user.get("id")
    result = await db.execute(
        select(Monitor.id).where(
            Monitor.id == notification.monitor_id,
            Monitor.owner_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


class NotificationResponse(BaseModel):
    id: int
    level: str
    title: str
    message: str
    monitor_id: Optional[int] = None
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    unread_count: int
    notifications: List[NotificationResponse]


@router.get("", response_model=NotificationListResponse)
@router.get("/", response_model=NotificationListResponse)
async def get_notifications(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Fetch recent notifications and total unread count."""
    filter_clause = _accessible_notification_filter(user)

    unread_query = select(func.count(Notification.id)).where(Notification.is_read.is_(False))
    if filter_clause is not None:
        unread_query = unread_query.where(filter_clause)
    unread_count = (await db.execute(unread_query)).scalar_one()

    query = select(Notification)
    if filter_clause is not None:
        query = query.where(filter_clause)
    query = query.order_by(desc(Notification.created_at)).limit(limit)
    res = await db.execute(query)
    notifications = res.scalars().all()

    return NotificationListResponse(
        unread_count=unread_count,
        notifications=[
            NotificationResponse(
                id=notification.id,
                level=notification.level,
                title=redact_sensitive_data(notification.title),
                message=redact_sensitive_data(notification.message),
                monitor_id=notification.monitor_id,
                is_read=notification.is_read,
                created_at=notification.created_at,
            )
            for notification in notifications
        ],
    )


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Mark a notification as read."""
    notif = await db.get(Notification, notification_id)
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not await _notification_accessible(db, user, notif):
        raise HTTPException(status_code=403, detail="Нет доступа к уведомлению")
    notif.is_read = True
    await db.commit()
    return {"message": "Marked as read"}


@router.post("/read-all")
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Mark all unread notifications as read."""
    filter_clause = _accessible_notification_filter(user)
    query = select(Notification).where(Notification.is_read.is_(False))
    if filter_clause is not None:
        query = query.where(filter_clause)
    res = await db.execute(query)
    for notif in res.scalars().all():
        notif.is_read = True
    await db.commit()
    return {"message": "All marked as read"}


@router.delete("")
@router.delete("/")
async def clear_notifications(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Clear all notifications."""
    filter_clause = _accessible_notification_filter(user)
    query = select(Notification)
    if filter_clause is not None:
        query = query.where(filter_clause)
    res = await db.execute(query)
    for notif in res.scalars().all():
        await db.delete(notif)
    await db.commit()
    return {"message": "Notifications cleared"}