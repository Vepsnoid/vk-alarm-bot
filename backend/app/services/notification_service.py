import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.models import Notification
from app.core.redaction import redact_sensitive_data

logger = logging.getLogger(__name__)


class NotificationService:
    @classmethod
    async def create_notification(cls, db: AsyncSession, title: str, message: str, level: str = "error", monitor_id: Optional[int] = None) -> Notification:
        title = redact_sensitive_data(title)
        message = redact_sensitive_data(message)
        notification = Notification(title=title, message=message, level=level, monitor_id=monitor_id, is_read=False)
        db.add(notification)
        await db.commit()
        await db.refresh(notification)
        return notification