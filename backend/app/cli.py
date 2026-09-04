"""Служебная командная строка.

    docker compose run --rm api python -m app.cli gen-key
    docker compose exec api python -m app.cli create-admin --email admin@company.ru
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

import httpx
from sqlalchemy import select

from .auth.passwords import WeakPasswordError, hash_password, validate_password_policy
from .config import get_settings
from .crypto import SecretCipher, generate_key, generate_token, hash_token
from .db import dispose_engine, get_sessionmaker
from .models import Camera, Role, ShareLink, User


def _fail(message: str) -> None:
    print(f"ошибка: {message}", file=sys.stderr)
    raise SystemExit(1)


# ─── gen-key ─────────────────────────────────────────────────────────────────
def cmd_gen_key(_: argparse.Namespace) -> None:
    print(generate_key())


# ─── healthcheck (используется docker HEALTHCHECK) ───────────────────────────
def cmd_healthcheck(args: argparse.Namespace) -> None:
    try:
        response = httpx.get(args.url, timeout=4.0)
    except httpx.HTTPError as exc:
        _fail(str(exc))
        return
    if response.status_code != 200:
        _fail(f"{args.url} -> {response.status_code}")
    print("ok")


# ─── create-admin / reset-password ───────────────────────────────────────────
def _read_password(confirm: bool = True) -> str:
    password = getpass.getpass("Пароль: ")
    if confirm and password != getpass.getpass("Повторите пароль: "):
        _fail("пароли не совпадают")
    return password


async def _create_user(email: str, role: Role, password: str, full_name: str) -> None:
    email = email.strip().lower()
    try:
        validate_password_policy(password, email=email)
    except WeakPasswordError as exc:
        _fail(str(exc))

    async with get_sessionmaker()() as session:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            _fail(f"пользователь {email} уже существует")
        session.add(
            User(
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
            )
        )
        await session.commit()
    print(f"создан пользователь {email} с ролью {role.value}")


def cmd_create_admin(args: argparse.Namespace) -> None:
    password = args.password or _read_password()
    asyncio.run(
        _run(_create_user(args.email, Role(args.role), password, args.full_name or ""))
    )


async def _reset_password(email: str, password: str) -> None:
    email = email.strip().lower()
    try:
        validate_password_policy(password, email=email)
    except WeakPasswordError as exc:
        _fail(str(exc))

    async with get_sessionmaker()() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            _fail(f"пользователь {email} не найден")
            return
        user.password_hash = hash_password(password)
        user.failed_attempts = 0
        user.locked_until = None
        await session.commit()
    print(f"пароль пользователя {email} обновлён")


def cmd_reset_password(args: argparse.Namespace) -> None:
    asyncio.run(_run(_reset_password(args.email, args.password or _read_password())))


# ─── rotate-key ──────────────────────────────────────────────────────────────
async def _rotate_key(new_key_b64: str) -> None:
    import base64

    old_cipher = SecretCipher(get_settings().secret_key)
    try:
        new_key = base64.b64decode(new_key_b64.strip(), validate=True)
    except ValueError:
        _fail("новый ключ должен быть base64")
        return
    if len(new_key) != 32:
        _fail("новый ключ должен быть 32 байта после base64-декодирования")
    new_cipher = SecretCipher(new_key)

    async with get_sessionmaker()() as session:
        cameras = list(await session.scalars(select(Camera)))
        for camera in cameras:
            camera.rtsp_url_enc = new_cipher.encrypt(old_cipher.decrypt(camera.rtsp_url_enc))

        users = list(await session.scalars(select(User).where(User.totp_secret_enc.is_not(None))))
        for user in users:
            if user.totp_secret_enc is None:
                continue
            user.totp_secret_enc = new_cipher.encrypt(old_cipher.decrypt(user.totp_secret_enc))

        await session.commit()

    print(
        f"перешифровано: камер {len(cameras)}, TOTP-секретов {len(users)}.\n"
        "Теперь замените содержимое secrets/app_key на новый ключ и перезапустите api и worker."
    )


def cmd_rotate_key(args: argparse.Namespace) -> None:
    new_key = args.new_key or generate_key()
    if not args.new_key:
        print("новый ключ (сохраните его!):")
        print(new_key)
    asyncio.run(_run(_rotate_key(new_key)))


# ─── ссылки ──────────────────────────────────────────────────────────────────
async def _revoke_link(slug: str) -> None:
    import datetime as dt

    async with get_sessionmaker()() as session:
        link = await session.scalar(select(ShareLink).where(ShareLink.slug == slug))
        if link is None:
            _fail(f"ссылка {slug} не найдена")
            return
        link.revoked_at = dt.datetime.now(dt.UTC)
        await session.commit()

    # Кэш решения authz живёт до 20 секунд — сбрасываем его явно и отбираем
    # доступ у тех, кто смотрит прямо сейчас, чтобы отзыв был мгновенным.
    from .internal.authz import drop_link_viewers

    await drop_link_viewers(link.id)
    print(f"ссылка {slug} отозвана")


def cmd_revoke_link(args: argparse.Namespace) -> None:
    asyncio.run(_run(_revoke_link(args.slug)))


# ─── прочее ──────────────────────────────────────────────────────────────────
async def _run(coro: object) -> None:
    try:
        await coro  # type: ignore[misc]
    finally:
        await dispose_engine()


def cmd_gen_token(_: argparse.Namespace) -> None:
    token = generate_token()
    print(f"token: {token}")
    print(f"sha256: {hash_token(token)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="RTSP — служебные команды")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gen-key", help="сгенерировать ключ шифрования (base64)")
    p.set_defaults(func=cmd_gen_key)

    p = sub.add_parser("healthcheck", help="проверить, что процесс отвечает")
    p.add_argument("--url", default="http://127.0.0.1:8000/healthz")
    p.set_defaults(func=cmd_healthcheck)

    p = sub.add_parser("create-admin", help="создать пользователя")
    p.add_argument("--email", required=True)
    p.add_argument("--role", default=Role.admin.value, choices=[r.value for r in Role])
    p.add_argument("--full-name", default="")
    p.add_argument("--password", help="если не указан — будет запрошен интерактивно")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("reset-password", help="сбросить пароль пользователя")
    p.add_argument("--email", required=True)
    p.add_argument("--password")
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("rotate-key", help="перешифровать секреты новым ключом")
    p.add_argument("--new-key", help="base64; если не указан — будет сгенерирован")
    p.set_defaults(func=cmd_rotate_key)

    p = sub.add_parser("revoke-link", help="отозвать публичную ссылку")
    p.add_argument("--slug", required=True)
    p.set_defaults(func=cmd_revoke_link)

    p = sub.add_parser("gen-token", help="сгенерировать токен и его хеш (для отладки)")
    p.set_defaults(func=cmd_gen_token)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
