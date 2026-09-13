"""
Отправка уведомлений администраторам.
Используется из bot.py и conference.py.
"""
from config import ADMIN_IDS


def _display_name(from_user) -> str:
    if from_user is None:
        return "unknown"
    parts = []
    if from_user.first_name:
        parts.append(from_user.first_name)
    if from_user.last_name:
        parts.append(from_user.last_name)
    name = " ".join(parts).strip()
    if from_user.username:
        name += f" (@{from_user.username})"
    if not name:
        name = f"id{from_user.id}"
    return f"{name} [id={from_user.id}]"


async def notify_admins(bot, text: str):
    """Отправляет текстовое сообщение всем админам. Молча игнорирует ошибки."""
    if not ADMIN_IDS:
        return
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            print(f"[Notify] Не удалось отправить админу {admin_id}: {e}")