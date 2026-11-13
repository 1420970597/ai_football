"""Telegram collector helpers that need no client, session or collector state."""
from __future__ import annotations

from datetime import timezone


class SourceAccessError(RuntimeError):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.details = details


def config_int(value) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _utc(value):
    if value.tzinfo is None:
        raise ValueError("Telegram timestamp must include timezone")
    return value.astimezone(timezone.utc)


def _trusted_message_author(chat_id, source, message):
    """Keep the durable author boundary available before optional Telegram I/O.

    Use only explicitly trusted stable peers. Broadcast posts may omit the
    sender entirely. A group can also explicitly trust its collective posting
    peer, but this never identifies the person behind an anonymous message.
    """
    chat_id = str(chat_id)
    sender_id = str(getattr(message, "sender_id", "") or "")
    trusted_sender_ids = {str(value) for value in source.get("trusted_sender_ids") or ()}
    channel_post_identity = chat_id in trusted_sender_ids and (
        sender_id == chat_id
        or (not sender_id and bool(getattr(message, "post", False)))
        # Profiles can sign broadcast posts. Preserve already configured
        # personal identities; use channel trust only for verified posts.
        or (source.get('verified_broadcast') is True and bool(getattr(message, 'post', False))
            and sender_id not in trusted_sender_ids))
    if channel_post_identity:
        sender_id = chat_id
    return (sender_id, channel_post_identity) if sender_id in trusted_sender_ids else None


def _message_revision_evidence(message):
    return (message.id, str(message.date), str(getattr(message, 'edit_date', None)),
            getattr(message, 'message', ''), getattr(message, 'reply_to_msg_id', None),
            getattr(getattr(getattr(message, 'media', None), 'photo', None), 'id', None),
            getattr(getattr(getattr(message, 'media', None), 'document', None), 'id', None),
            getattr(getattr(message, 'media', None), 'id', None))


def _nickname(sender, sender_id: str) -> str:
    return (" ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")]))
            or getattr(sender, "title", "") or getattr(sender, "username", "") or sender_id)[:120]
