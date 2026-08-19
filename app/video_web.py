from __future__ import annotations

import html
import hashlib
import json
import logging
import random
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.models import (
    AllowedUser,
    Chunk,
    DirectorAssignment,
    DirectorReminderLog,
    Document,
    DocumentStatusEnum,
    FeedbackResponse,
    Homework,
    User,
    UserEvent,
    UserNotificationSetting,
    VisibilityEnum,
)
from app.db.repositories import ProgramMediaRepository
from app.db.session import SessionLocal
from app.services.director_dashboard import DIRECTOR_DISPLAY_NAMES
from app.services.video_links import verify_director_dashboard_token, verify_video_watch_token

logger = logging.getLogger(__name__)

DIRECTOR_DASHBOARD_ENABLED = True
DIRECTOR_REMINDER_COOLDOWN_MINUTES = 30
DIRECTOR_PLACEHOLDER_DIR = "director_placeholders"
DIRECTOR_DASHBOARD_DATA_PATH = Path("director_dashboard") / "leader_dashboard_data.json"


def resolve_stored_file_path(stored_path: str | None) -> Path | None:
    if not stored_path:
        return None
    path = Path(stored_path)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def validate_watch_token(request: web.Request, media_id: int) -> bool:
    settings = request.app["settings"]
    return verify_video_watch_token(
        media_id=media_id,
        expires_raw=request.query.get("expires"),
        token=request.query.get("token"),
        secret=settings.video_link_secret,
    )


async def load_video_media(media_id: int):
    async with SessionLocal() as session:
        media = await ProgramMediaRepository.get_by_id(session, media_id)
    if media is None or media.media_type != "video":
        return None
    return media


def lesson_filter_for(model, lesson_key: str | None, lesson_date) -> object | None:
    if lesson_key and lesson_date:
        return and_(model.lesson_key == lesson_key, model.lesson_date == lesson_date)
    if lesson_key:
        return model.lesson_key == lesson_key
    if lesson_date:
        return model.lesson_date == lesson_date
    return None


async def load_related_content(media) -> tuple[list[Document], list[Homework], list[tuple[Document, str]]]:
    lesson_filter = lesson_filter_for(Document, media.lesson_key, media.lesson_date)
    homework_filter = lesson_filter_for(Homework, media.lesson_key, media.lesson_date)
    if lesson_filter is None:
        return [], [], []

    async with SessionLocal() as session:
        docs_result = await session.execute(
            select(Document)
            .where(
                and_(
                    Document.status == DocumentStatusEnum.ready,
                    Document.visibility == VisibilityEnum.global_,
                    lesson_filter,
                )
            )
            .order_by(Document.material_type.asc().nulls_last(), Document.created_at.desc())
            .limit(12)
        )
        docs = list(docs_result.scalars().all())

        homeworks: list[Homework] = []
        if homework_filter is not None:
            homework_result = await session.execute(
                select(Homework)
                .where(and_(Homework.status == "active", homework_filter))
                .order_by(Homework.deadline_date.asc().nulls_last(), Homework.id.asc())
                .limit(5)
            )
            homeworks = list(homework_result.scalars().all())

        summary_snippets: list[tuple[Document, str]] = []
        summary_docs = [doc for doc in docs if doc.material_type == "summary"][:2]
        for document in summary_docs:
            chunks_result = await session.execute(
                select(Chunk.chunk_text)
                .where(Chunk.document_id == document.id)
                .order_by(Chunk.chunk_index.asc())
                .limit(2)
            )
            text = "\n\n".join(chunk.strip() for chunk in chunks_result.scalars().all() if chunk and chunk.strip())
            if text:
                summary_snippets.append((document, shorten_text(text, 760)))

    return docs, homeworks, summary_snippets


def shorten_text(text: str, limit: int = 520) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    cut_at = clean.rfind(" ", 0, limit)
    if cut_at < limit // 2:
        cut_at = limit
    return f"{clean[:cut_at].rstrip()}..."


def format_date(value) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def material_type_label(material_type: str | None) -> str:
    labels = {
        "summary": "Саммари",
        "homework": "Домашнее задание",
        "materials": "Материал",
        "transcript": "Транскрипция",
        "schedule": "Расписание",
    }
    return labels.get(material_type or "", "Материал")


def build_related_html(docs: list[Document], homeworks: list[Homework], summary_snippets: list[tuple[Document, str]]) -> str:
    material_docs = [doc for doc in docs if doc.material_type not in {"summary", "homework", "transcript"}]

    summary_html = ""
    if summary_snippets:
        cards = []
        for document, snippet in summary_snippets:
            cards.append(
                "<article class=\"info-card info-card-wide\">"
                "<div class=\"eyebrow\">Саммари</div>"
                f"<h2>{html.escape(document.title)}</h2>"
                f"<p>{html.escape(snippet)}</p>"
                "</article>"
            )
        summary_html = "".join(cards)

    materials_html = ""
    if material_docs:
        items = []
        for document in material_docs[:6]:
            doc_date = f" · {html.escape(format_date(document.lesson_date))}" if document.lesson_date else ""
            items.append(
                "<li>"
                f"<span>{html.escape(document.title)}</span>"
                f"<small>{html.escape(material_type_label(document.material_type))}{doc_date}</small>"
                "</li>"
            )
        materials_html = (
            "<article class=\"info-card\">"
            "<div class=\"eyebrow\">Материалы</div>"
            "<h2>К этой записи</h2>"
            f"<ul class=\"resource-list\">{''.join(items)}</ul>"
            "<p class=\"note\">Оригиналы файлов можно скачать в боте: Материалы программы → занятие → Материалы и презентации.</p>"
            "</article>"
        )

    homework_html = ""
    if homeworks:
        blocks = []
        for homework in homeworks:
            deadline = f"Срок сдачи: {html.escape(format_date(homework.deadline_date))}" if homework.deadline_date else "Срок сдачи уточняется"
            link = ""
            if homework.moodle_url:
                safe_url = html.escape(homework.moodle_url, quote=True)
                link = f"<a class=\"action-link\" href=\"{safe_url}\" target=\"_blank\" rel=\"noopener\">Открыть задание в ПРОГРЕССе</a>"
            blocks.append(
                "<div class=\"homework-item\">"
                f"<h3>{html.escape(homework.title)}</h3>"
                f"<p>{deadline}</p>"
                f"{link}"
                "</div>"
            )
        homework_html = (
            "<article class=\"info-card\">"
            "<div class=\"eyebrow\">Домашнее задание</div>"
            f"{''.join(blocks)}"
            "</article>"
        )

    if not (summary_html or materials_html or homework_html):
        return (
            "<section class=\"related empty\">"
            "<article class=\"info-card\">"
            "<div class=\"eyebrow\">Материалы</div>"
            "<h2>Связанные материалы скоро появятся</h2>"
            "<p>Когда организаторы добавят саммари, презентации или домашнее задание, они будут отображаться здесь.</p>"
            "</article>"
            "</section>"
        )

    return f"<section class=\"related\">{summary_html}{materials_html}{homework_html}</section>"


def parse_director_telegram_id(request: web.Request) -> int | None:
    raw_value = request.query.get("telegram_id")
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None


def validate_director_dashboard_request(request: web.Request, telegram_id: int) -> bool:
    settings = request.app["settings"]
    return verify_director_dashboard_token(
        telegram_id=telegram_id,
        expires_raw=request.query.get("expires"),
        token=request.query.get("token"),
        secret=settings.video_link_secret,
    )


def format_datetime(value) -> str:
    if value is None:
        return "нет данных"
    return value.strftime("%d.%m.%Y %H:%M")


def notification_label(enabled, notification_time) -> str:
    if enabled is True:
        return f"включены, {notification_time}"
    if enabled is False:
        return "отключены"
    return "не выбрано время"


def activity_label(last_activity) -> str:
    if last_activity is None:
        return "не заходил"
    return format_datetime(last_activity)


def html_badge(text: str, tone: str = "neutral") -> str:
    return f"<span class=\"badge badge-{tone}\">{html.escape(text)}</span>"


def participant_display_name(row) -> str:
    return (
        row.allowed_full_name
        or row.user_full_name
        or (f"@{row.allowed_username.lstrip('@')}" if row.allowed_username else None)
        or (f"@{row.user_username.lstrip('@')}" if row.user_username else None)
        or str(row.telegram_id)
    )


async def load_director_dashboard_data(telegram_id: int, admin_ids: list[int]) -> dict:
    event_stats = (
        select(
            UserEvent.telegram_id.label("telegram_id"),
            func.max(UserEvent.created_at).label("last_activity"),
            func.count(UserEvent.id).label("event_count"),
        )
        .group_by(UserEvent.telegram_id)
        .subquery()
    )
    feedback_stats = (
        select(
            FeedbackResponse.user_id.label("user_id"),
            func.count(FeedbackResponse.id).filter(FeedbackResponse.status == "completed").label("feedback_completed"),
            func.count(FeedbackResponse.id).filter(FeedbackResponse.status == "not_attended").label("feedback_not_attended"),
            func.count(FeedbackResponse.id).filter(FeedbackResponse.status == "pending").label("feedback_pending"),
        )
        .group_by(FeedbackResponse.user_id)
        .subquery()
    )

    async with SessionLocal() as session:
        viewer = (
            await session.execute(select(User).where(User.telegram_id == telegram_id).limit(1))
        ).scalar_one_or_none()
        viewer_allowed = (
            await session.execute(select(AllowedUser).where(AllowedUser.telegram_id == telegram_id).limit(1))
        ).scalar_one_or_none()
        is_admin = telegram_id in set(admin_ids)

        base_stmt = (
            select(
                AllowedUser.telegram_id.label("telegram_id"),
                AllowedUser.username.label("allowed_username"),
                AllowedUser.full_name.label("allowed_full_name"),
                User.id.label("user_id"),
                User.username.label("user_username"),
                User.full_name.label("user_full_name"),
                User.created_at.label("registered_at"),
                UserNotificationSetting.enabled.label("notifications_enabled"),
                UserNotificationSetting.notification_time.label("notification_time"),
                event_stats.c.last_activity,
                event_stats.c.event_count,
                feedback_stats.c.feedback_completed,
                feedback_stats.c.feedback_not_attended,
                feedback_stats.c.feedback_pending,
            )
            .select_from(AllowedUser)
            .outerjoin(User, User.telegram_id == AllowedUser.telegram_id)
            .outerjoin(UserNotificationSetting, UserNotificationSetting.user_id == User.id)
            .outerjoin(event_stats, event_stats.c.telegram_id == AllowedUser.telegram_id)
            .outerjoin(feedback_stats, feedback_stats.c.user_id == User.id)
            .where(AllowedUser.is_active.is_(True), AllowedUser.telegram_id.is_not(None))
        )

        explicit_assignment_count = int(
            (
                await session.execute(
                    select(func.count(DirectorAssignment.id)).where(
                        and_(
                            DirectorAssignment.director_telegram_id == telegram_id,
                            DirectorAssignment.is_active.is_(True),
                        )
                    )
                )
            ).scalar()
            or 0
        )

        if explicit_assignment_count > 0:
            base_stmt = base_stmt.join(
                DirectorAssignment,
                and_(
                    DirectorAssignment.employee_telegram_id == AllowedUser.telegram_id,
                    DirectorAssignment.director_telegram_id == telegram_id,
                    DirectorAssignment.is_active.is_(True),
                ),
            )
            mode = "director"
            anonymize = False
        elif is_admin:
            if admin_ids:
                base_stmt = base_stmt.where(~AllowedUser.telegram_id.in_(admin_ids))
            mode = "admin_test"
            anonymize = True
        else:
            base_stmt = base_stmt.join(
                DirectorAssignment,
                and_(
                    DirectorAssignment.employee_telegram_id == AllowedUser.telegram_id,
                    DirectorAssignment.director_telegram_id == telegram_id,
                    DirectorAssignment.is_active.is_(True),
                ),
            )
            mode = "director"
            anonymize = False

        rows = list((await session.execute(base_stmt.order_by(AllowedUser.full_name.asc().nulls_last()))).all())

        current_homework_count = int(
            (
                await session.execute(
                    select(func.count(Homework.id)).where(
                        and_(
                            Homework.status == "active",
                            Homework.deadline_date.is_not(None),
                            Homework.deadline_date >= date.today(),
                        )
                    )
                )
            ).scalar()
            or 0
        )

    if viewer is None and viewer_allowed is None and not is_admin:
        return {"allowed": False, "reason": "viewer_not_found"}

    if not is_admin and not rows:
        return {"allowed": False, "reason": "no_team"}

    return {
        "allowed": True,
        "mode": mode,
        "anonymize": anonymize,
        "viewer": viewer,
        "viewer_allowed": viewer_allowed,
        "rows": rows,
        "current_homework_count": current_homework_count,
    }


def render_director_dashboard_html(data: dict) -> str:
    rows = data["rows"]
    total = len(rows)
    registered = sum(1 for row in rows if row.user_id is not None)
    notifications_on = sum(1 for row in rows if row.notifications_enabled is True)
    feedback_completed = sum(int(row.feedback_completed or 0) for row in rows)
    current_homework_count = int(data.get("current_homework_count") or 0)
    anonymize = bool(data.get("anonymize"))
    table_rows = []
    for index, row in enumerate(rows, start=1):
        person_name = f"Участник {index}" if anonymize else participant_display_name(row)
        person_detail = "данные обезличены" if anonymize else str(row.telegram_id)
        registered_badge = html_badge("зашёл", "good") if row.user_id else html_badge("не заходил", "warn")
        notifications_tone = "good" if row.notifications_enabled is True else "warn"
        feedback_text = f"ОС: {int(row.feedback_completed or 0)}"
        if row.feedback_not_attended:
            feedback_text += f", не был: {int(row.feedback_not_attended)}"
        table_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(person_name)}</strong><small>{html.escape(person_detail)}</small></td>"
            f"<td>{registered_badge}</td>"
            f"<td>{html_badge(notification_label(row.notifications_enabled, row.notification_time), notifications_tone)}</td>"
            f"<td>{html.escape(feedback_text)}</td>"
            "<td><span class=\"muted\">ждём выгрузку посещаемости</span></td>"
            "<td><span class=\"muted\">ждём выгрузку ПРОГРЕССа</span></td>"
            "</tr>"
        )

    if not table_rows:
        table_rows.append(
            "<tr><td colspan=\"6\" class=\"empty\">Команда пока не назначена. Данные появятся после загрузки связки директор-сотрудники.</td></tr>"
        )

    html_body = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Кабинет руководителя</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap&subset=cyrillic" rel="stylesheet">
  <style>
    :root {{
      --bg: #f7f7f3;
      --surface: #fff;
      --text: #08080a;
      --muted: #686b72;
      --line: rgba(8, 8, 10, .13);
      --accent: #704ceb;
      --green: #43b264;
      --red: #e55145;
      --yellow: #f3b341;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Montserrat", "Segoe UI", Arial, sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    .page {{ width: min(1180px, 100%); margin: 0 auto; padding: clamp(14px, 3vw, 36px); }}
    .hero {{
      padding: clamp(24px, 5vw, 56px);
      border: 1px solid var(--line);
      background: var(--surface);
    }}
    .eyebrow {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 12px 0 0;
      font-size: clamp(32px, 6vw, 72px);
      line-height: .98;
      letter-spacing: -.06em;
      text-transform: uppercase;
    }}
    .note {{ max-width: 780px; margin: 22px 0 0; color: var(--muted); font-size: 15px; line-height: 1.65; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 1px;
      margin-top: 18px;
      background: var(--line);
      border: 1px solid var(--line);
    }}
    .metric {{ min-height: 132px; padding: 20px; background: var(--surface); }}
    .metric strong {{ display: block; font-size: 38px; letter-spacing: -.05em; }}
    .metric span {{ display: block; margin-top: 8px; color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }}
    .section {{
      margin-top: 18px;
      border: 1px solid var(--line);
      background: var(--surface);
      overflow-x: auto;
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 860px; }}
    th, td {{ padding: 16px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 14px; }}
    th {{ color: var(--muted); font-size: 11px; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }}
    td small {{ display: block; margin-top: 5px; color: var(--muted); font-size: 11px; }}
    .badge {{ display: inline-flex; padding: 7px 10px; border: 1px solid var(--line); font-size: 12px; font-weight: 800; }}
    .badge-good {{ border-color: rgba(67,178,100,.35); color: #16783a; background: rgba(67,178,100,.08); }}
    .badge-warn {{ border-color: rgba(229,81,69,.35); color: #a43129; background: rgba(229,81,69,.08); }}
    .badge-neutral {{ color: var(--muted); }}
    .muted, .empty {{ color: var(--muted); }}
    @media (max-width: 760px) {{
      .page {{ padding: 0; }}
      .hero, .section {{ border-right: 0; border-left: 0; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); border-right: 0; border-left: 0; }}
      .metric {{ min-height: 112px; }}
      .metric strong {{ font-size: 30px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="eyebrow">Лига Лидеров</div>
      <h1>Кабинет руководителя</h1>
    </section>
    <section class="grid">
      <article class="metric"><strong>{total}</strong><span>в команде</span></article>
      <article class="metric"><strong>{registered}</strong><span>зашли в бота</span></article>
      <article class="metric"><strong>{notifications_on}</strong><span>включили уведомления</span></article>
      <article class="metric"><strong>{feedback_completed}</strong><span>анкет ОС заполнено</span></article>
      <article class="metric"><strong>{current_homework_count}</strong><span>домашек сейчас</span></article>
    </section>
    <section class="section">
      <table>
        <thead>
          <tr>
            <th>Сотрудник</th>
            <th>Бот</th>
            <th>Уведомления</th>
            <th>Обратная связь</th>
            <th>Посещение</th>
            <th>Домашки</th>
          </tr>
        </thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </section>
  </main>
</body>
</html>"""
    return html_body


def director_placeholder_files(settings) -> list[Path]:
    directory = settings.data_dir / DIRECTOR_PLACEHOLDER_DIR
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.gif") if path.is_file())


def choose_director_placeholder(settings) -> Path | None:
    files = director_placeholder_files(settings)
    if not files:
        return None
    return random.choice(files)


def render_director_placeholder_html(settings) -> str:
    placeholder = choose_director_placeholder(settings)
    image_html = (
        f'<img class="placeholder-gif" src="/director/placeholders/{html.escape(placeholder.name, quote=True)}" '
        'alt="Раздел в работе">'
        if placeholder is not None
        else '<div class="placeholder-empty">Раздел в работе</div>'
    )
    html_body = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Кабинет руководителя</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap&subset=cyrillic" rel="stylesheet">
  <style>
    :root {{
      --bg: #f7f7f3;
      --surface: #fff;
      --text: #08080a;
      --muted: #686b72;
      --line: rgba(8, 8, 10, .13);
      --accent: #704ceb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Montserrat", "Segoe UI", Arial, sans-serif;
      color: var(--text);
      background: var(--bg);
    }}
    .page {{
      width: min(900px, 100%);
      min-height: 100vh;
      margin: 0 auto;
      padding: clamp(14px, 3vw, 36px);
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    .hero {{
      padding: clamp(24px, 5vw, 52px);
      border: 1px solid var(--line);
      background: var(--surface);
    }}
    .eyebrow {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 12px 0 0;
      font-size: clamp(34px, 8vw, 76px);
      line-height: .96;
      letter-spacing: -.06em;
      text-transform: uppercase;
    }}
    .placeholder-card {{
      flex: 1;
      display: grid;
      place-items: center;
      min-height: 420px;
      padding: clamp(20px, 5vw, 52px);
      border: 1px solid var(--line);
      background: var(--surface);
    }}
    .placeholder-gif {{
      display: block;
      width: min(540px, 100%);
      max-height: 58vh;
      object-fit: contain;
    }}
    .placeholder-empty {{
      padding: 18px 22px;
      border: 1px solid var(--line);
      color: var(--muted);
      font-weight: 700;
    }}
    @media (max-width: 760px) {{
      .page {{ padding: 0; }}
      .hero, .placeholder-card {{ border-right: 0; border-left: 0; }}
      .placeholder-card {{ min-height: 360px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="eyebrow">Лига Лидеров</div>
      <h1>Кабинет руководителя</h1>
    </section>
    <section class="placeholder-card">
      {image_html}
    </section>
  </main>
</body>
</html>"""
    return html_body


def load_director_dashboard_source(settings) -> dict | None:
    data_path = settings.data_dir / DIRECTOR_DASHBOARD_DATA_PATH
    if not data_path.exists():
        logger.warning("director_dashboard_data_missing path=%s", data_path)
        return None
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("director_dashboard_data_read_failed path=%s error=%s", data_path, exc)
        return None


def sanitize_director_cell(cell: dict) -> dict:
    safe_cell = deepcopy(cell)
    detail = str(safe_cell.get("detail") or "")
    detail = re.sub(r"\s*·\s*Отправил\(а\):\s*[^·]+$", "", detail).strip()
    detail = re.sub(r"Отправил\(а\):\s*[^·]+", "Отправил(а): участник команды", detail).strip()
    safe_cell["detail"] = detail
    return safe_cell


def director_name_tokens(value: str | None) -> set[str]:
    normalized = (value or "").lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9]+", " ", normalized)
    return {token for token in normalized.split() if token}


def director_person_matches(row, person: dict) -> bool:
    row_tokens = director_name_tokens(participant_display_name(row))
    person_tokens = director_name_tokens(str(person.get("name") or ""))
    if len(row_tokens) < 2 or len(person_tokens) < 2:
        return False
    return len(row_tokens & person_tokens) >= 2


def build_missing_director_person(row) -> dict:
    return {
        "id": str(row.telegram_id),
        "telegramId": row.telegram_id,
        "name": participant_display_name(row),
        "department": "нет данных в последней выгрузке",
        "score": 0,
        "available": 0,
        "seasonMax": 0,
        "percent": 0,
        "certificate": "нет данных",
        "cells": [],
    }


def build_real_director_progress_data(settings, data: dict, telegram_id: int, admin_ids: list[int]) -> dict | None:
    source = load_director_dashboard_source(settings)
    if source is None:
        return None

    is_admin = telegram_id in set(admin_ids)
    neuro_placeholder = choose_director_placeholder(settings)
    if is_admin:
        people_source = list(source.get("participants") or [])
        team_title = "Все сотрудники программы"
        director_label = DIRECTOR_DISPLAY_NAMES.get(telegram_id, str(telegram_id))
    else:
        people_source = []
        used_source_ids: set[str] = set()
        for row in data.get("rows") or []:
            matched_person = None
            for person in source.get("participants") or []:
                source_id = str(person.get("id") or "")
                if source_id in used_source_ids:
                    continue
                if director_person_matches(row, person):
                    matched_person = person
                    used_source_ids.add(source_id)
                    break
            people_source.append(matched_person or build_missing_director_person(row))

        viewer = data.get("viewer")
        viewer_allowed = data.get("viewer_allowed")
        viewer_name = (
            getattr(viewer, "full_name", None)
            or getattr(viewer, "username", None)
            or getattr(viewer_allowed, "full_name", None)
            or getattr(viewer_allowed, "username", None)
            or str(telegram_id)
        )
        team_title = f"Команда: {viewer_name}"
        director_label = viewer_name

    people = []
    for person in people_source:
        safe_person = deepcopy(person)
        safe_person["id"] = str(safe_person.get("id") or safe_person.get("telegramId") or safe_person.get("name") or "")
        safe_person["certificate"] = person.get("certificate") or "В плане"
        safe_person["cells"] = [sanitize_director_cell(cell) for cell in person.get("cells") or []]
        people.append(safe_person)

    return {
        "updatedAt": source.get("updatedAt") or "",
        "asOf": source.get("asOf") or "",
        "teamTitle": team_title,
        "directorLabel": director_label,
        "participants": people,
        "points": source.get("points") or [],
        "neuroOsPlaceholder": neuro_placeholder.name if neuro_placeholder is not None else None,
    }


def cell_type(cell: dict) -> str:
    return str(cell.get("type") or "")


def cell_tone(cell: dict) -> str:
    tone = str(cell.get("tone") or "planned")
    return tone if tone in {"done", "partial", "problem", "planned"} else "planned"


def cell_score_value(cell: dict) -> float | None:
    score = cell.get("score")
    if score is None:
        return None
    try:
        return float(score)
    except (TypeError, ValueError):
        return None


def person_cells_by_type(person: dict, type_name: str) -> list[dict]:
    return [cell for cell in person.get("cells") or [] if cell_type(cell) == type_name]


def person_issue_cells(person: dict) -> list[dict]:
    return [
        cell
        for cell in person.get("cells") or []
        if cell_type(cell) in {"ДЗ", "Занятие"} and cell_tone(cell) == "problem"
    ]


def person_attendance_summary(person: dict) -> tuple[int, int]:
    cells = person_cells_by_type(person, "Занятие")
    finished = [cell for cell in cells if cell_score_value(cell) is not None]
    attended = [cell for cell in finished if (cell_score_value(cell) or 0) > 0]
    return len(attended), len(finished)


def person_homework_summary(person: dict) -> tuple[int, int, int]:
    cells = person_cells_by_type(person, "ДЗ")
    due = [cell for cell in cells if cell_score_value(cell) is not None]
    done = [cell for cell in due if (cell_score_value(cell) or 0) > 0]
    missing = [cell for cell in due if (cell_score_value(cell) or 0) == 0]
    return len(done), len(due), len(missing)


def progress_tone(percent_value: int | float | None) -> str:
    value = int(percent_value or 0)
    if value < 50:
        return "problem"
    if value < 75:
        return "partial"
    return "done"


def status_label(tone: str) -> str:
    return {
        "done": "в норме",
        "partial": "есть нюансы",
        "problem": "нужно внимание",
        "planned": "в плане",
    }.get(tone, "в плане")


def build_reminder_text(person: dict) -> str:
    issues = person_issue_cells(person)
    homework_issues = [cell for cell in issues if cell_type(cell) == "ДЗ"]
    lesson_issues = [cell for cell in issues if cell_type(cell) == "Занятие"]
    if homework_issues:
        titles = "; ".join(str(cell.get("title") or "домашнее задание") for cell in homework_issues[:2])
        return (
            "Привет! Напоминаю про активности по программе «Лига Лидеров». "
            f"Есть задание, которое стоит закрыть: {titles}. "
            "Если нужна запись, материалы или ссылка на ПРОГРЕСС, открой бота-помощника."
        )
    if lesson_issues:
        titles = "; ".join(str(cell.get("title") or "занятие") for cell in lesson_issues[:2])
        return (
            "Привет! Похоже, стоит вернуться к материалам по занятию: "
            f"{titles}. Запись и материалы можно открыть через бота-помощника."
        )
    return (
        "Привет! Спасибо, что двигаешься по программе «Лига Лидеров». "
        "Если захочешь освежить материалы, записи и домашние задания доступны в боте-помощнике."
    )


def render_status_dot(tone: str, label: str | int | float | None) -> str:
    safe_tone = cell_tone({"tone": tone})
    text = "" if label is None else str(label)
    return f'<span class="llm-dot is-{safe_tone}">{html.escape(text)}</span>'


def render_person_detail_groups(person: dict) -> str:
    groups = [
        ("Входной этап", person_cells_by_type(person, "Входной этап")),
        ("Занятия", person_cells_by_type(person, "Занятие")),
        ("Итоги посещаемости", person_cells_by_type(person, "Итого занятий")),
        ("Домашние задания", person_cells_by_type(person, "ДЗ")),
    ]
    blocks = []
    for title, cells in groups:
        if not cells:
            continue
        items = []
        for cell in cells:
            items.append(
                "<li>"
                f"{render_status_dot(cell_tone(cell), cell.get('display'))}"
                "<div>"
                f"<strong>{html.escape(str(cell.get('title') or cell.get('label') or title))}</strong>"
                f"<span>{html.escape(str(cell.get('status') or ''))}</span>"
                f"<small>{html.escape(str(cell.get('detail') or ''))}</small>"
                "</div>"
                "</li>"
            )
        blocks.append(
            "<section class=\"llm-detail-block\">"
            f"<h3>{html.escape(title)}</h3>"
            f"<ul>{''.join(items)}</ul>"
            "</section>"
        )
    return "".join(blocks)


def director_reminder_status_label(status: str) -> str:
    return {
        "sent": "отправлено",
        "partial": "частично",
        "failed": "ошибка",
    }.get(status, status or "неизвестно")


def render_director_reminder_journal(logs: list[dict]) -> str:
    if not logs:
        return """
    <section class="llm-journal">
      <div class="llm-journal-head">
        <h2>Журнал напоминаний</h2>
        <span>пока пусто</span>
      </div>
      <p class="llm-journal-empty">Здесь появятся тестовые отправки: кто сформировал напоминание, для какого сотрудника и какой текст был отправлен администраторам.</p>
    </section>
"""

    rows = []
    for log in logs:
        text_preview = str(log.get("reminderText") or "").strip()
        sent_count = int(log.get("sentCount") or 0)
        failed_count = int(log.get("failedCount") or 0)
        delivery_note = f"админам: {sent_count}"
        if failed_count:
            delivery_note += f", ошибок: {failed_count}"
        rows.append(
            "<tr>"
            f"<td>{html.escape(format_datetime(log.get('createdAt')))}</td>"
            f"<td>{html.escape(str(log.get('employeeName') or 'Сотрудник'))}</td>"
            f"<td>{html.escape(director_reminder_status_label(str(log.get('status') or '')))}</td>"
            f"<td>{html.escape(delivery_note)}</td>"
            "<td>"
            "<details class=\"llm-journal-text\">"
            "<summary>текст</summary>"
            f"<p>{html.escape(text_preview)}</p>"
            "</details>"
            "</td>"
            "</tr>"
        )

    return f"""
    <section class="llm-journal">
      <div class="llm-journal-head">
        <h2>Журнал напоминаний</h2>
        <span>последние {len(rows)}</span>
      </div>
      <div class="llm-journal-table">
        <table>
          <thead>
            <tr>
              <th>Когда</th>
              <th>Кому</th>
              <th>Статус</th>
              <th>Режим</th>
              <th>Шаблон</th>
            </tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
"""


def render_director_progress_dashboard_html(data: dict) -> str:
    people = data["participants"]
    total_people = len(people)
    earned = sum(int(person.get("score") or 0) for person in people)
    available = sum(int(person.get("available") or 0) for person in people)
    average = round((earned / available) * 100) if available else 0
    homework_done = 0
    homework_total = 0
    homework_missing = 0
    attendance_done = 0
    attendance_total = 0
    needs_attention = 0
    for person in people:
        done, total, missing = person_homework_summary(person)
        homework_done += done
        homework_total += total
        homework_missing += missing
        attended, attended_total = person_attendance_summary(person)
        attendance_done += attended
        attendance_total += attended_total
        if person_issue_cells(person):
            needs_attention += 1

    cards = []
    for person in people:
        percent_value = int(person.get("percent") or 0)
        tone = progress_tone(percent_value)
        attended, attended_total = person_attendance_summary(person)
        done, total, missing = person_homework_summary(person)
        issues = person_issue_cells(person)
        reminder_text = build_reminder_text(person)
        issue_hint = (
            f"{len(issues)} зон внимания"
            if issues
            else "без критичных зон"
        )
        cards.append(
            "<article class=\"llm-person-card\">"
            "<div class=\"llm-person-top\">"
            "<div>"
            f"<h2>{html.escape(str(person.get('name') or 'Сотрудник'))}</h2>"
            f"<p>{html.escape(str(person.get('department') or data.get('teamTitle') or 'Команда'))}</p>"
            "</div>"
            f"<span class=\"llm-person-status is-{tone}\">{status_label(tone)}</span>"
            "</div>"
            "<div class=\"llm-progress-row\">"
            f"<strong>{percent_value}%</strong>"
            f"<span>{html.escape(str(person.get('score') or 0))}/{html.escape(str(person.get('available') or 0))} баллов</span>"
            "</div>"
            f"<div class=\"llm-progress\"><span class=\"is-{tone}\" style=\"width:{max(0, min(100, percent_value))}%\"></span></div>"
            "<div class=\"llm-chip-row\">"
            f"<span>Посещаемость {attended}/{attended_total}</span>"
            f"<span>ДЗ {done}/{total}</span>"
            f"<span>{html.escape(issue_hint)}</span>"
            "</div>"
            "<div class=\"llm-card-actions\">"
            f"<button type=\"button\" data-reminder-name=\"{html.escape(str(person.get('name') or ''), quote=True)}\" "
            f"data-reminder-draft=\"{html.escape(reminder_text, quote=True)}\">Сформировать напоминание</button>"
            "</div>"
            "<details>"
            "<summary>Открыть детализацию</summary>"
            f"{render_person_detail_groups(person)}"
            "</details>"
            "</article>"
        )

    point_headers = [
        f"<th title=\"{html.escape(str(point.get('title') or ''))}\">{html.escape(str(point.get('label') or ''))}</th>"
        for point in data.get("points") or []
    ]
    matrix_rows = []
    for person in people:
        cells_by_id = {str(cell.get("id")): cell for cell in person.get("cells") or []}
        matrix_cells = []
        for point in data.get("points") or []:
            cell = cells_by_id.get(str(point.get("id")))
            if cell is None:
                matrix_cells.append("<td></td>")
            else:
                matrix_cells.append(
                    f"<td title=\"{html.escape(str(cell.get('status') or ''))} · {html.escape(str(cell.get('detail') or ''))}\">"
                    f"{render_status_dot(cell_tone(cell), cell.get('display'))}"
                    "</td>"
                )
        matrix_rows.append(
            "<tr>"
            f"<td class=\"llm-matrix-name\">{html.escape(str(person.get('name') or 'Сотрудник'))}</td>"
            f"<td>{html.escape(str(person.get('score') or 0))}/{html.escape(str(person.get('available') or 0))}</td>"
            f"{''.join(matrix_cells)}"
            "</tr>"
        )

    neuro_placeholder_name = data.get("neuroOsPlaceholder")
    if neuro_placeholder_name:
        neuro_media_html = (
            f'<img src="/director/placeholders/{html.escape(str(neuro_placeholder_name), quote=True)}" '
            'alt="Раздел AI feedback готовится">'
        )
    else:
        neuro_media_html = '<div class="llm-neuro-empty">Раздел готовится</div>'

    reminder_journal_html = render_director_reminder_journal(data.get("reminderLogs") or [])

    html_body = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Кабинет руководителя</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap&subset=cyrillic" rel="stylesheet">
  <style>
    :root {{
      --bg: #ffffff;
      --ink: #121239;
      --muted: #5f6076;
      --line: #121239;
      --soft: #f2f2f2;
      --purple: #7949f4;
      --violet: #c252f7;
      --blue: #002fa7;
      --sky: #58c0ed;
      --red-brand: #f9423a;
      --yellow-brand: #f9c546;
      --paper: #f7f5ff;
      --dark: #121239;
      --green: #71f270;
      --yellow: #f9c546;
      --red: #f9423a;
      --gray: #a2acab;
      --shadow: 6px 6px 0 var(--ink);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(90deg, rgba(121,73,244,.05) 1px, transparent 1px),
        linear-gradient(180deg, rgba(18,18,57,.045) 1px, transparent 1px),
        var(--bg);
      background-size: 22px 22px;
      color: var(--ink);
      font-family: "Montserrat", "Segoe UI", Arial, sans-serif;
    }}
    .llm-page {{
      width: min(1180px, 100%);
      margin: 0 auto;
      padding: clamp(14px, 4vw, 34px);
    }}
    .llm-hero {{
      padding: clamp(22px, 5vw, 46px);
      border: 3px solid var(--line);
      border-radius: 28px;
      background: var(--paper);
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}
    .llm-hero h1 {{
      margin: 0;
      max-width: 790px;
      font-size: clamp(34px, 8vw, 74px);
      line-height: .96;
      letter-spacing: -.06em;
      text-transform: uppercase;
      position: relative;
      z-index: 1;
    }}
    .llm-hero-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 18px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
      position: relative;
      z-index: 1;
    }}
    .llm-hero-meta span {{
      padding: 9px 12px;
      border: 2px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
    }}
    .llm-kpis {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin-top: 22px;
    }}
    .llm-kpi {{
      min-height: 118px;
      padding: 18px;
      border: 3px solid var(--line);
      border-radius: 22px;
      background: #fff;
      box-shadow: 4px 4px 0 var(--ink);
    }}
    .llm-kpi:nth-child(2) {{ background: var(--paper); }}
    .llm-kpi:nth-child(3) {{ background: #fff; }}
    .llm-kpi:nth-child(4) {{ background: #f7f5ff; }}
    .llm-kpi:nth-child(5) {{ background: #fff1f0; }}
    .llm-kpi:nth-child(5) strong {{ color: var(--red-brand); }}
    .llm-kpi strong {{
      display: block;
      font-size: clamp(28px, 5vw, 44px);
      line-height: .95;
      letter-spacing: -.05em;
    }}
    .llm-kpi span {{
      display: block;
      margin-top: 10px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .llm-section-title {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      margin: 34px 0 14px;
      padding: 9px 13px;
      border: 3px solid var(--line);
      border-radius: 999px;
      background: #fff;
      box-shadow: 4px 4px 0 var(--ink);
      font-size: 18px;
      letter-spacing: -.03em;
      text-transform: uppercase;
    }}
    .llm-section-title::before {{
      content: "";
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: var(--purple);
      border: 2px solid var(--ink);
    }}
    .llm-cards {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .llm-person-card {{
      border: 3px solid var(--line);
      border-radius: 24px;
      background: #fff;
      padding: 18px;
      box-shadow: 5px 5px 0 var(--ink);
      position: relative;
      overflow: hidden;
    }}
    .llm-person-card::before {{
      content: "";
      position: absolute;
      inset: 0 0 auto 0;
      height: 8px;
      background: var(--cyan);
    }}
    .llm-person-card:nth-child(2n)::before {{ background: var(--violet); }}
    .llm-person-card:nth-child(3n)::before {{ background: var(--red-brand); }}
    .llm-person-card > * {{
      position: relative;
      z-index: 1;
    }}
    .llm-person-top {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: flex-start;
    }}
    .llm-person-card h2 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.1;
      letter-spacing: -.04em;
    }}
    .llm-person-card p {{
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .llm-person-status {{
      flex: 0 0 auto;
      padding: 8px 10px;
      border: 2px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .llm-person-status.is-done {{ color: #0f5f35; background: #efffed; }}
    .llm-person-status.is-partial {{ color: #6b5400; background: #fff4cf; }}
    .llm-person-status.is-problem {{ color: #8c1623; background: #fff1f0; }}
    .llm-progress-row {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-top: 18px;
    }}
    .llm-progress-row strong {{ font-size: 38px; letter-spacing: -.06em; }}
    .llm-progress-row span {{ color: var(--muted); font-size: 12px; font-weight: 800; }}
    .llm-progress {{
      height: 10px;
      margin-top: 8px;
      border: 2px solid var(--ink);
      border-radius: 999px;
      background: #fff;
      overflow: hidden;
    }}
    .llm-progress span {{ display: block; height: 100%; }}
    .llm-progress .is-done {{ background: var(--green); }}
    .llm-progress .is-partial {{ background: var(--yellow); }}
    .llm-progress .is-problem {{ background: var(--red); }}
    .llm-chip-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 14px;
    }}
    .llm-chip-row span {{
      padding: 7px 9px;
      border: 2px solid rgba(18, 18, 57, .18);
      border-radius: 999px;
      background: var(--soft);
      color: #2a2a54;
      font-size: 12px;
      font-weight: 800;
    }}
    .llm-card-actions {{
      display: flex;
      gap: 8px;
      margin-top: 14px;
    }}
    .llm-card-actions button,
    .llm-modal-actions button {{
      min-height: 44px;
      border: 3px solid var(--ink);
      border-radius: 999px;
      background: var(--purple);
      color: #ffffff;
      padding: 10px 15px;
      font-weight: 800;
      cursor: pointer;
      box-shadow: 3px 3px 0 var(--ink);
      transition: transform .15s ease, box-shadow .15s ease, background .15s ease;
    }}
    .llm-card-actions button:hover,
    .llm-modal-actions button:hover {{
      background: var(--violet);
      transform: translate(-1px, -1px);
      box-shadow: 5px 5px 0 var(--ink);
    }}
    .llm-card-actions button:active,
    .llm-modal-actions button:active {{
      transform: translate(2px, 2px);
      box-shadow: 1px 1px 0 var(--ink);
    }}
    .llm-card-actions button:disabled,
    .llm-modal-actions button:disabled {{
      cursor: progress;
      opacity: .68;
      transform: none;
      box-shadow: 2px 2px 0 var(--ink);
    }}
    details {{
      margin-top: 14px;
      border-top: 2px solid rgba(18, 18, 57, .14);
      padding-top: 12px;
    }}
    summary {{
      cursor: pointer;
      color: var(--purple);
      font-size: 13px;
      font-weight: 800;
    }}
    .llm-detail-block {{
      margin-top: 12px;
      border: 2px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
    }}
    .llm-detail-block h3 {{
      margin: 0;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--soft);
      font-size: 13px;
    }}
    .llm-detail-block ul {{
      display: grid;
      gap: 0;
      list-style: none;
      margin: 0;
      padding: 0;
    }}
    .llm-detail-block li {{
      display: grid;
      grid-template-columns: 34px minmax(0, 1fr);
      gap: 10px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
    }}
    .llm-detail-block li:last-child {{ border-bottom: 0; }}
    .llm-detail-block strong {{ display: block; font-size: 12px; }}
    .llm-detail-block span,
    .llm-detail-block small {{ display: block; margin-top: 3px; color: var(--muted); font-size: 11px; line-height: 1.35; }}
    .llm-dot {{
      width: 28px;
      height: 28px;
      display: inline-grid;
      place-items: center;
      border: 3px solid var(--gray);
      background: #fff;
      font-size: 11px;
      font-weight: 800;
    }}
    .llm-dot.is-done {{ border-color: var(--green); }}
    .llm-dot.is-partial {{ border-color: var(--yellow); }}
    .llm-dot.is-problem {{ border-color: var(--red); }}
    .llm-dot.is-planned {{ border-color: var(--gray); background: #f5f6f8; color: #98a2b3; }}
    .llm-journal {{
      margin-top: 22px;
      border: 3px solid var(--line);
      border-radius: 24px;
      background: #fff;
      box-shadow: 5px 5px 0 var(--ink);
      overflow: hidden;
    }}
    .llm-journal-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 2px solid var(--line);
      background: var(--paper);
    }}
    .llm-journal-head h2 {{
      margin: 0;
      font-size: 20px;
      letter-spacing: -.04em;
    }}
    .llm-journal-head span {{
      padding: 7px 10px;
      border: 2px solid var(--ink);
      border-radius: 999px;
      background: var(--purple);
      color: #ffffff;
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .llm-journal-empty {{
      margin: 0;
      padding: 16px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      font-weight: 700;
    }}
    .llm-journal-table {{
      overflow: auto;
    }}
    .llm-journal table {{
      width: 100%;
      min-width: 680px;
      border-collapse: separate;
      border-spacing: 0;
    }}
    .llm-journal th,
    .llm-journal td {{
      padding: 10px 12px;
      border-right: 1px solid rgba(18, 18, 57, .12);
      border-bottom: 1px solid rgba(18, 18, 57, .12);
      text-align: left;
      vertical-align: top;
      font-size: 12px;
    }}
    .llm-journal th {{
      background: var(--soft);
      color: #424a57;
      font-size: 11px;
      text-transform: uppercase;
    }}
    .llm-journal-text {{
      margin: 0;
      padding: 0;
      border-top: 0;
    }}
    .llm-journal-text summary {{
      color: var(--purple);
      font-size: 12px;
    }}
    .llm-journal-text p {{
      max-width: 440px;
      margin: 8px 0 0;
      color: var(--ink);
      white-space: pre-wrap;
      line-height: 1.45;
    }}
    .llm-matrix {{
      margin-top: 14px;
      border: 3px solid var(--line);
      border-radius: 24px;
      overflow: auto;
      background: #fff;
      box-shadow: 5px 5px 0 var(--ink);
    }}
    .llm-matrix table {{
      width: max-content;
      min-width: 100%;
      border-collapse: separate;
      border-spacing: 0;
    }}
    .llm-matrix th,
    .llm-matrix td {{
      padding: 8px;
      border-right: 1px solid rgba(18, 18, 57, .14);
      border-bottom: 1px solid rgba(18, 18, 57, .14);
      text-align: center;
      font-size: 12px;
      white-space: nowrap;
    }}
    .llm-matrix th {{
      position: sticky;
      top: 0;
      z-index: 2;
      background: var(--paper);
      color: #424a57;
      font-size: 11px;
    }}
    .llm-matrix-name {{
      position: sticky;
      left: 0;
      z-index: 1;
      min-width: 190px;
      text-align: left !important;
      background: #fff;
      font-weight: 800;
    }}
    .llm-neuro {{
      display: grid;
      grid-template-columns: minmax(0, .95fr) minmax(220px, .7fr);
      gap: 16px;
      margin-top: 28px;
      border: 3px solid var(--ink);
      border-radius: 28px;
      background: var(--dark);
      color: #fff;
      box-shadow: 6px 6px 0 var(--ink);
      overflow: hidden;
    }}
    .llm-neuro-copy {{
      padding: clamp(20px, 4vw, 34px);
    }}
    .llm-neuro h2 {{
      margin: 0;
      font-size: clamp(34px, 9vw, 72px);
      line-height: .88;
      letter-spacing: -.07em;
      text-transform: uppercase;
    }}
    .llm-neuro p {{
      margin: 16px 0 0;
      max-width: 520px;
      color: rgba(255,255,255,.76);
      font-size: 14px;
      line-height: 1.55;
      font-weight: 600;
    }}
    .llm-neuro-tag {{
      display: inline-flex;
      margin-top: 18px;
      padding: 9px 12px;
      border: 2px solid #fff;
      border-radius: 999px;
      background: var(--purple);
      color: #ffffff;
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      box-shadow: 3px 3px 0 #fff;
    }}
    .llm-neuro-media {{
      min-height: 260px;
      display: grid;
      place-items: center;
      padding: 18px;
      background:
        radial-gradient(circle at 20% 20%, rgba(121,73,244,.18), transparent 30%),
        radial-gradient(circle at 80% 80%, rgba(194,82,247,.16), transparent 32%),
        #fff;
    }}
    .llm-neuro-media img {{
      width: min(100%, 360px);
      max-height: 300px;
      object-fit: contain;
      border: 3px solid var(--ink);
      border-radius: 22px;
      background: #fff;
      box-shadow: 5px 5px 0 var(--ink);
    }}
    .llm-neuro-empty {{
      padding: 18px;
      border: 3px solid var(--ink);
      border-radius: 22px;
      background: var(--paper);
      color: var(--ink);
      font-weight: 900;
    }}
    .llm-modal[hidden] {{ display: none; }}
    .llm-modal {{
      position: fixed;
      inset: 0;
      z-index: 99999;
      display: grid;
      place-items: center;
      padding: 18px;
      background: rgba(17,24,39,.48);
    }}
    .llm-dialog {{
      width: min(560px, 100%);
      border: 3px solid var(--line);
      border-radius: 24px;
      background: #fff;
      padding: 18px;
      box-shadow: 8px 8px 0 var(--ink);
    }}
    .llm-dialog h2 {{ margin: 0; font-size: 22px; letter-spacing: -.04em; }}
    .llm-dialog p {{ color: var(--muted); font-size: 13px; line-height: 1.5; }}
    .llm-dialog-note {{
      padding: 11px 12px;
      border: 2px solid var(--ink);
      border-radius: 16px;
      background: var(--paper);
      color: var(--ink) !important;
      font-weight: 700;
    }}
    .llm-send-status {{
      min-height: 20px;
      margin: 10px 0 0;
      font-weight: 800;
    }}
    .llm-send-status.is-ok {{ color: #0f7a41; }}
    .llm-send-status.is-error {{ color: #b42334; }}
    .llm-reminder-text {{
      width: 100%;
      min-height: 140px;
      resize: vertical;
      border: 2px solid var(--line);
      border-radius: 18px;
      padding: 12px;
      font: inherit;
      font-size: 13px;
      line-height: 1.45;
    }}
    .llm-modal-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }}
    .llm-modal-actions .is-secondary {{
      background: #fff;
      color: var(--ink);
      border: 3px solid var(--line);
    }}
    @media (max-width: 820px) {{
      .llm-page {{ padding: 10px; }}
      .llm-hero, .llm-person-card, .llm-journal, .llm-matrix, .llm-neuro {{
        border-width: 2px;
        border-radius: 22px;
        box-shadow: 4px 4px 0 var(--ink);
      }}
      .llm-kpis {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .llm-cards {{ grid-template-columns: 1fr; gap: 10px; }}
      .llm-section-title {{ margin-left: 2px; }}
      .llm-person-card {{ padding: 14px; }}
      .llm-person-top {{ display: grid; }}
      .llm-person-status {{ justify-self: start; }}
      .llm-progress-row strong {{ font-size: 32px; }}
      .llm-card-actions button {{ width: 100%; }}
      .llm-neuro {{
        grid-template-columns: 1fr;
        margin-top: 18px;
      }}
      .llm-neuro-media {{
        min-height: 220px;
        padding: 14px;
      }}
      .llm-neuro-media img {{
        max-height: 240px;
      }}
      .llm-matrix {{ max-height: 56vh; }}
      .llm-modal {{ padding: 8px; place-items: stretch; }}
      .llm-dialog {{ width: 100%; min-height: auto; }}
    }}
  </style>
</head>
<body>
  <main class="llm-page">
    <section class="llm-hero">
      <h1>Кабинет руководителя</h1>
      <div class="llm-hero-meta">
        <span>{html.escape(str(data.get("teamTitle") or "Команда"))}</span>
        <span>Данные: {html.escape(str(data.get("updatedAt") or "не указано"))}</span>
      </div>
    </section>
    <section class="llm-kpis">
      <article class="llm-kpi"><strong>{total_people}</strong><span>сотрудников</span></article>
      <article class="llm-kpi"><strong>{average}%</strong><span>средний прогресс</span></article>
      <article class="llm-kpi"><strong>{attendance_done}/{attendance_total}</strong><span>посещения</span></article>
      <article class="llm-kpi"><strong>{homework_done}/{homework_total}</strong><span>домашние задания</span></article>
      <article class="llm-kpi"><strong>{needs_attention}</strong><span>нужно внимание</span></article>
    </section>
    <h2 class="llm-section-title">Сотрудники</h2>
    <section class="llm-cards">
      {''.join(cards)}
    </section>
    {reminder_journal_html}
    <section class="llm-neuro" id="ai-feedback" aria-label="AI feedback">
      <div class="llm-neuro-copy">
        <h2>AI feedback</h2>
        <p>Будущий раздел для аккуратной управленческой обратной связи: безопасная сводка по учебным фактам и черновики формулировок для разговора с сотрудником.</p>
        <span class="llm-neuro-tag">Раздел готовится</span>
      </div>
      <div class="llm-neuro-media">
        {neuro_media_html}
      </div>
    </section>
    <h2 class="llm-section-title">Матрица прохождения</h2>
    <section class="llm-matrix" aria-label="Матрица прохождения программы">
      <table>
        <thead>
          <tr>
            <th class="llm-matrix-name">Сотрудник</th>
            <th>Баллы</th>
            {''.join(point_headers)}
          </tr>
        </thead>
        <tbody>{''.join(matrix_rows)}</tbody>
      </table>
    </section>
  </main>
  <div class="llm-modal" hidden data-reminder-modal>
    <div class="llm-dialog" role="dialog" aria-modal="true" aria-labelledby="reminder-title">
      <h2 id="reminder-title">Черновик напоминания</h2>
      <p class="llm-dialog-note">Боевой смысл формы: этот текст будет отправлен выбранному сотруднику. Тестовый режим: сейчас сообщение уйдёт тебе и всем администраторам для проверки, а реальному сотруднику оно пока не отправляется.</p>
      <p data-reminder-person></p>
      <textarea class="llm-reminder-text" data-reminder-output></textarea>
      <p class="llm-send-status" data-reminder-status></p>
      <div class="llm-modal-actions">
        <button type="button" data-send-reminder>Отправить тест админам</button>
        <button type="button" data-copy-reminder>Скопировать текст</button>
        <button type="button" class="is-secondary" data-close-reminder>Закрыть</button>
      </div>
    </div>
  </div>
  <script>
    (function() {{
      const modal = document.querySelector("[data-reminder-modal]");
      const personNode = document.querySelector("[data-reminder-person]");
      const textNode = document.querySelector("[data-reminder-output]");
      const statusNode = document.querySelector("[data-reminder-status]");
      const closeButton = document.querySelector("[data-close-reminder]");
      const copyButton = document.querySelector("[data-copy-reminder]");
      const sendButton = document.querySelector("[data-send-reminder]");
      document.querySelectorAll("[data-reminder-draft]").forEach((button) => {{
        button.addEventListener("click", () => {{
          personNode.textContent = button.dataset.reminderName || "";
          textNode.value = button.dataset.reminderDraft || "";
          statusNode.textContent = "";
          statusNode.className = "llm-send-status";
          sendButton.disabled = false;
          modal.hidden = false;
        }});
      }});
      closeButton.addEventListener("click", () => {{ modal.hidden = true; }});
      modal.addEventListener("click", (event) => {{
        if (event.target === modal) modal.hidden = true;
      }});
      copyButton.addEventListener("click", async () => {{
        textNode.select();
        try {{
          await navigator.clipboard.writeText(textNode.value);
          copyButton.textContent = "Скопировано";
          setTimeout(() => {{ copyButton.textContent = "Скопировать текст"; }}, 1400);
        }} catch (error) {{
          document.execCommand("copy");
        }}
      }});
      sendButton.addEventListener("click", async () => {{
        statusNode.textContent = "Отправляю тестовое уведомление администраторам...";
        statusNode.className = "llm-send-status";
        sendButton.disabled = true;
        try {{
          const response = await fetch(`/director/reminder${{window.location.search}}`, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              employee_name: personNode.textContent || "",
              text: textNode.value || ""
            }})
          }});
          const payload = await response.json().catch(() => ({{}}));
          if (!response.ok) {{
            throw new Error(payload.message || "Не удалось отправить тестовое уведомление");
          }}
          statusNode.textContent = payload.message || "Тестовое уведомление отправлено администраторам.";
          statusNode.className = "llm-send-status is-ok";
        }} catch (error) {{
          statusNode.textContent = error.message || "Не удалось отправить тестовое уведомление";
          statusNode.className = "llm-send-status is-error";
        }} finally {{
          sendButton.disabled = false;
        }}
      }});
    }})();
  </script>
</body>
</html>"""
    return html_body


def truncate_telegram_text(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def director_reminder_hash(reminder_text: str) -> str:
    return hashlib.sha256(reminder_text.strip().encode("utf-8")).hexdigest()


async def load_recent_director_reminder_log(
    director_telegram_id: int,
    employee_name: str,
) -> DirectorReminderLog | None:
    cooldown_since = datetime.now(timezone.utc) - timedelta(minutes=DIRECTOR_REMINDER_COOLDOWN_MINUTES)
    async with SessionLocal() as session:
        result = await session.execute(
            select(DirectorReminderLog)
            .where(
                and_(
                    DirectorReminderLog.director_telegram_id == director_telegram_id,
                    DirectorReminderLog.employee_name == employee_name,
                    DirectorReminderLog.created_at >= cooldown_since,
                    DirectorReminderLog.status.in_(["sent", "partial"]),
                )
            )
            .order_by(DirectorReminderLog.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def load_director_reminder_logs(director_telegram_id: int, limit: int = 10) -> list[dict]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(DirectorReminderLog)
            .where(DirectorReminderLog.director_telegram_id == director_telegram_id)
            .order_by(DirectorReminderLog.created_at.desc())
            .limit(limit)
        )
        logs = result.scalars().all()
    return [
        {
            "createdAt": log.created_at,
            "employeeName": log.employee_name,
            "templateKey": log.template_key,
            "reminderText": log.reminder_text,
            "deliveryMode": log.delivery_mode,
            "status": log.status,
            "sentCount": len(log.sent_recipient_ids or []),
            "failedCount": len(log.failed_recipient_ids or []),
        }
        for log in logs
    ]


async def save_director_reminder_log(
    *,
    director_telegram_id: int,
    employee_name: str,
    reminder_text: str,
    sent_ids: list[int],
    failed_ids: list[str],
) -> None:
    if sent_ids and failed_ids:
        status = "partial"
    elif sent_ids:
        status = "sent"
    else:
        status = "failed"
    async with SessionLocal() as session:
        session.add(
            DirectorReminderLog(
                director_telegram_id=director_telegram_id,
                employee_name=employee_name,
                template_key="director_attention_reminder",
                reminder_text=reminder_text,
                reminder_hash=director_reminder_hash(reminder_text),
                delivery_mode="admin_test",
                status=status,
                sent_recipient_ids=sent_ids,
                failed_recipient_ids=failed_ids,
            )
        )
        await session.commit()


async def director_reminder_test_handler(request: web.Request) -> web.Response:
    telegram_id = parse_director_telegram_id(request)
    if telegram_id is None:
        raise web.HTTPForbidden(text="Не удалось определить пользователя")
    if not validate_director_dashboard_request(request, telegram_id):
        raise web.HTTPForbidden(text="Ссылка устарела. Открой кабинет из бота ещё раз.")

    settings = request.app["settings"]
    source_data = await load_director_dashboard_data(telegram_id, settings.admin_ids)
    if not source_data.get("allowed"):
        raise web.HTTPForbidden(text="Тестовая отправка недоступна для этого пользователя")

    data = build_real_director_progress_data(settings, source_data, telegram_id, settings.admin_ids)
    if data is None:
        raise web.HTTPForbidden(text="Тестовая отправка недоступна для этого пользователя")

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"message": "Не удалось прочитать данные формы"}, status=400)

    employee_name = str(payload.get("employee_name") or "").strip()
    reminder_text = str(payload.get("text") or "").strip()
    allowed_employee_names = {str(person.get("name") or "").strip() for person in data.get("participants") or []}
    if not employee_name or employee_name not in allowed_employee_names:
        return web.json_response({"message": "Не удалось определить сотрудника из команды"}, status=400)
    if not reminder_text:
        return web.json_response({"message": "Текст уведомления пустой"}, status=400)

    recent_log = await load_recent_director_reminder_log(telegram_id, employee_name)
    if recent_log is not None:
        return web.json_response(
            {
                "message": (
                    "По этому сотруднику уже было тестовое напоминание "
                    f"{format_datetime(recent_log.created_at)}. "
                    f"Чтобы не заспамить, повтор доступен через {DIRECTOR_REMINDER_COOLDOWN_MINUTES} минут."
                )
            },
            status=429,
        )

    director_name = DIRECTOR_DISPLAY_NAMES.get(telegram_id, f"админ {telegram_id}")
    message_text = truncate_telegram_text(
        "\n".join(
            [
                "Тестовый режим. Реальному сотруднику сообщение не отправлено.",
                "",
                f"Уведомление от твоего руководителя: {director_name}",
                f"Кому в боевом режиме: {employee_name}",
                "",
                "Текст уведомления:",
                reminder_text,
            ]
        )
    )
    recipient_ids = sorted(set(settings.admin_ids))

    sent_ids: list[int] = []
    failed: list[str] = []
    bot = Bot(token=settings.telegram_bot_token)
    try:
        for recipient_id in recipient_ids:
            try:
                await bot.send_message(
                    chat_id=recipient_id,
                    text=message_text,
                    disable_web_page_preview=True,
                )
                sent_ids.append(recipient_id)
            except TelegramAPIError as exc:
                logger.warning(
                    "director_test_reminder_send_failed director_id=%s recipient_id=%s error=%s",
                    telegram_id,
                    recipient_id,
                    exc,
                )
                failed.append(str(recipient_id))
    finally:
        await bot.session.close()

    log_saved = True
    try:
        await save_director_reminder_log(
            director_telegram_id=telegram_id,
            employee_name=employee_name,
            reminder_text=reminder_text,
            sent_ids=sent_ids,
            failed_ids=failed,
        )
    except SQLAlchemyError as exc:
        log_saved = False
        logger.warning(
            "director_test_reminder_log_failed director_id=%s employee_name=%s error=%s",
            telegram_id,
            employee_name,
            exc,
        )

    if not sent_ids:
        return web.json_response(
            {
                "message": (
                    "Telegram не принял тестовое уведомление ни для одного администратора. "
                    + ("Попытка записана в журнал." if log_saved else "Журнал тоже не записался, проверим вручную.")
                )
            },
            status=502,
        )

    log_suffix = " Запись добавлена в журнал." if log_saved else " Уведомление ушло, но журнал не записался."
    if failed:
        return web.json_response(
            {
                "message": (
                    f"Тест отправлен администраторам: {len(sent_ids)}. "
                    f"Не удалось отправить: {len(failed)}."
                    f"{log_suffix}"
                ),
                "sent": sent_ids,
                "failed": failed,
            },
            status=207,
        )

    return web.json_response(
        {
            "message": f"Тестовое уведомление отправлено администраторам: {len(sent_ids)}.{log_suffix}",
            "sent": sent_ids,
        }
    )


async def director_dashboard_handler(request: web.Request) -> web.Response:
    telegram_id = parse_director_telegram_id(request)
    if telegram_id is None:
        raise web.HTTPForbidden(text="Не удалось определить пользователя")
    if not validate_director_dashboard_request(request, telegram_id):
        raise web.HTTPForbidden(text="Ссылка устарела. Открой кабинет из бота ещё раз.")

    settings = request.app["settings"]
    if not DIRECTOR_DASHBOARD_ENABLED:
        return web.Response(
            body=render_director_placeholder_html(settings).encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    data = await load_director_dashboard_data(telegram_id, settings.admin_ids)
    if not data.get("allowed"):
        raise web.HTTPForbidden(text="Кабинет руководителя недоступен для этого пользователя")
    progress_data = build_real_director_progress_data(settings, data, telegram_id, settings.admin_ids)
    if progress_data is not None:
        progress_data["reminderLogs"] = await load_director_reminder_logs(telegram_id)
        return web.Response(
            body=render_director_progress_dashboard_html(progress_data).encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    return web.Response(
        body=render_director_dashboard_html(data).encode("utf-8"),
        content_type="text/html",
        charset="utf-8",
    )


async def director_placeholder_asset_handler(request: web.Request) -> web.StreamResponse:
    filename = Path(request.match_info["filename"]).name
    settings = request.app["settings"]
    file_path = settings.data_dir / DIRECTOR_PLACEHOLDER_DIR / filename
    if not file_path.exists() or not file_path.is_file() or file_path.suffix.lower() != ".gif":
        raise web.HTTPNotFound(text="Файл не найден")
    return web.FileResponse(
        file_path,
        headers={
            "Cache-Control": "private, max-age=60",
        },
    )


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def watch_handler(request: web.Request) -> web.Response:
    try:
        media_id = int(request.match_info["media_id"])
    except ValueError:
        raise web.HTTPNotFound(text="Видео не найдено")

    if not validate_watch_token(request, media_id):
        raise web.HTTPForbidden(text="Ссылка на видео устарела. Открой материал в боте ещё раз.")

    media = await load_video_media(media_id)
    if media is None:
        raise web.HTTPNotFound(text="Видео не найдено")

    file_path = resolve_stored_file_path(media.stored_path)
    if file_path is None or not file_path.exists() or not file_path.is_file():
        logger.warning("video_file_missing media_id=%s path=%s", media.id, media.stored_path)
        raise web.HTTPNotFound(text="Файл видео не найден")

    query = request.query_string
    video_src = f"/video/{media.id}"
    if query:
        video_src = f"{video_src}?{query}"

    docs, homeworks, summary_snippets = await load_related_content(media)
    related_html = build_related_html(docs, homeworks, summary_snippets)

    page_title = html.escape(media.title)
    safe_video_src = html.escape(video_src, quote=True)
    date_badge = f"<span>Дата: {html.escape(format_date(media.lesson_date))}</span>" if media.lesson_date else ""
    module_badge = f"<span>{html.escape(media.module_title)}</span>" if media.module_title else ""
    badges = f"{date_badge}{module_badge}"

    html_body = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{page_title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap&subset=cyrillic" rel="stylesheet">
  <style>
    :root {{
      --bg: #f7f7f3;
      --surface: #ffffff;
      --surface-soft: rgba(8, 8, 10, 0.035);
      --text: #08080a;
      --muted: #62666d;
      --line: rgba(8, 8, 10, 0.16);
      --line-soft: rgba(8, 8, 10, 0.07);
      --accent: #e55145;
      --accent-violet: #704ceb;
      --accent-green: #43b264;
      --accent-cyan: #1d8db4;
      --navy: #111136;
    }}
    * {{ box-sizing: border-box; }}
    html {{ min-height: 100%; background: var(--bg); }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Montserrat", "Segoe UI", Arial, sans-serif;
      color: var(--text);
      background: var(--bg);
      color-scheme: light;
    }}
    .page {{
      width: min(1180px, 100%);
      margin: 0 auto;
      padding: clamp(12px, 3vw, 42px);
    }}
    .card {{
      position: relative;
      overflow: hidden;
      min-height: calc(100vh - clamp(24px, 6vw, 84px));
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 0;
      box-shadow: none;
    }}
    .card::before,
    .card::after {{
      position: absolute;
      top: 0;
      bottom: 0;
      z-index: 0;
      width: 1px;
      content: "";
      background: var(--line-soft);
      pointer-events: none;
    }}
    .card::before {{ left: 25%; }}
    .card::after {{ right: 25%; }}
    .header,
    .video-section,
    .related {{
      position: relative;
      z-index: 1;
    }}
    .header {{
      padding: clamp(24px, 5vw, 64px);
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      max-width: 980px;
      margin: 0;
      color: var(--text);
      font-size: clamp(28px, 5.2vw, 64px);
      line-height: 0.98;
      letter-spacing: -0.055em;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: clamp(22px, 4vw, 36px);
    }}
    .badges span {{
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 8px 13px;
      border: 1px solid var(--line);
      background: #ffffff;
      color: var(--text);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.08em;
      line-height: 1.1;
      text-transform: uppercase;
    }}
    .video-section {{
      padding: clamp(14px, 3vw, 36px) clamp(12px, 3vw, 42px) clamp(22px, 4vw, 46px);
      border-bottom: 1px solid var(--line);
    }}
    .video-frame {{
      padding: 8px;
      background: #08080a;
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 18px 48px rgba(8, 8, 10, 0.14);
    }}
    video {{
      display: block;
      width: 100%;
      max-height: min(72vh, 720px);
      border-radius: 12px;
      background: #000;
      outline: none;
    }}
    .helper {{
      max-width: 760px;
      margin-top: 20px;
      padding-left: 18px;
      border-left: 1px solid var(--accent);
      color: var(--muted);
      font-size: 15px;
      line-height: 1.65;
    }}
    .helper p {{ margin: 0; }}
    .related {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1px;
      padding: 0;
      background: var(--line);
    }}
    .info-card {{
      padding: clamp(20px, 3vw, 32px);
      background: var(--surface);
      border: 0;
      border-radius: 0;
    }}
    .info-card-wide {{ grid-column: 1 / -1; }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 16px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.14em;
    }}
    .eyebrow::before {{
      width: 8px;
      height: 8px;
      content: "";
      background: var(--accent);
    }}
    .info-card h2, .homework-item h3 {{
      margin: 0 0 14px;
      color: var(--text);
      font-size: clamp(20px, 2.2vw, 30px);
      line-height: 1.08;
      letter-spacing: -0.045em;
    }}
    .info-card p, .homework-item p {{
      margin: 0;
      color: #303238;
      font-size: 15px;
      line-height: 1.72;
    }}
    .resource-list {{
      display: grid;
      gap: 14px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .resource-list li {{
      padding-left: 16px;
      border-left: 1px solid var(--accent-cyan);
    }}
    .resource-list span {{
      display: block;
      color: var(--text);
      font-size: 15px;
      font-weight: 700;
      line-height: 1.45;
    }}
    .resource-list small {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .note {{ margin-top: 14px !important; color: var(--muted) !important; }}
    .homework-item + .homework-item {{
      margin-top: 18px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
    }}
    .action-link {{
      display: inline-flex;
      align-items: center;
      min-height: 44px;
      margin-top: 16px;
      padding: 11px 15px;
      border: 1px solid var(--text);
      background: transparent;
      color: var(--text);
      text-decoration: none;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      transition: color 180ms ease, background-color 180ms ease, border-color 180ms ease;
    }}
    .action-link:hover {{
      color: #ffffff;
      border-color: var(--accent-green);
      background: var(--accent-green);
    }}
    @media (max-width: 720px) {{
      .page {{ padding: 0; }}
      .card {{ min-height: 100vh; border-right: 0; border-left: 0; }}
      .card::before {{ left: 50%; }}
      .card::after {{ display: none; }}
      .header {{ padding: 26px 18px 28px; }}
      h1 {{ font-size: clamp(26px, 8.8vw, 38px); line-height: 1.02; }}
      .badges span {{ min-height: 32px; font-size: 11px; }}
      .video-section {{ padding: 12px 12px 24px; }}
      .video-frame {{ padding: 5px; border-radius: 14px; }}
      video {{ border-radius: 10px; max-height: 62vh; }}
      .helper {{ padding-left: 14px; font-size: 13px; }}
      .related {{ grid-template-columns: 1fr; }}
      .info-card {{ padding: 20px 18px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <article class="card">
      <header class="header">
        <h1>{page_title}</h1>
        <div class="badges">{badges}</div>
      </header>
      <section class="video-section">
        <div class="video-frame">
          <video controls preload="metadata" playsinline src="{safe_video_src}"></video>
        </div>
        <div class="helper">
          <p>Если видео не открывается, открой ссылку ещё раз из бота или напиши организаторам программы.</p>
        </div>
      </section>
      {related_html}
    </article>
  </main>
</body>
</html>"""
    return web.Response(
        body=html_body.encode("utf-8"),
        content_type="text/html",
        charset="utf-8",
    )


async def video_handler(request: web.Request) -> web.StreamResponse:
    try:
        media_id = int(request.match_info["media_id"])
    except ValueError:
        raise web.HTTPNotFound(text="Видео не найдено")

    if not validate_watch_token(request, media_id):
        raise web.HTTPForbidden(text="Ссылка на видео устарела. Открой материал в боте ещё раз.")

    media = await load_video_media(media_id)
    if media is None:
        raise web.HTTPNotFound(text="Видео не найдено")

    file_path = resolve_stored_file_path(media.stored_path)
    if file_path is None or not file_path.exists() or not file_path.is_file():
        logger.warning("video_file_missing media_id=%s path=%s", media.id, media.stored_path)
        raise web.HTTPNotFound(text="Файл видео не найден")

    return web.FileResponse(
        file_path,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        },
    )


def create_app() -> web.Application:
    settings = get_settings()
    app = web.Application()
    app["settings"] = settings
    app.router.add_get("/health", health_handler)
    app.router.add_get("/director", director_dashboard_handler)
    app.router.add_post("/director/reminder", director_reminder_test_handler)
    app.router.add_get("/director/placeholders/{filename}", director_placeholder_asset_handler)
    app.router.add_get("/watch/{media_id}", watch_handler)
    app.router.add_get("/video/{media_id}", video_handler)
    return app


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    settings = get_settings()
    web.run_app(create_app(), host=settings.video_web_host, port=settings.video_web_port)


if __name__ == "__main__":
    main()
