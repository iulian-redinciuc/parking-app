"""P7.1: admin login (api.md §4): password → session token, the 5 / 15 min login limit,
`ADMIN_TOKEN` or a session token on admin routes, expiry and logout, and the hash CLI."""

import contextlib
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from argon2 import PasswordHasher
from sqlmodel import Session, select
from typer.testing import CliRunner

from parking.api.app import create_app
from parking.api.routes.admin import verify_password
from parking.cli import app as cli
from parking.config import Settings
from parking.core.clock import FakeClock
from parking.db.engine import make_engine
from parking.db.models import AdminSession

PASSWORD = "correct horse battery"
STATIC = "static-admin-token-0123456789abcdef"
# cheap parameters keep the tests fast; verify reads them from the hash
HASH = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

LOT_YAML = """
version: 1
lot: {id: main, name: Parking, location: {lat: 1.5, lon: 2.5}, timezone: UTC}
zones:
  - {id: underground, name: {en: Underground}, method: flow, capacity: 60}
cameras:
  - id: cam-ramp
    role: flow
    zones: [underground]
    source: "file:y.jpg"
    lines_file: config/lines/cam-ramp.json
    detector: {model: models/x}
"""


@pytest.fixture
def lot(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "lot.yaml").write_text(LOT_YAML)
    return tmp_path


@pytest.fixture
def clock():
    return FakeClock(T0)


def make_app(lot, clock, **settings):
    values = {"admin_password_hash": HASH, "admin_token": STATIC} | settings
    return create_app(
        lot / "config" / "lot.yaml",
        Settings(_env_file=None, **values),
        clock=clock,
        db_url=f"sqlite:///{lot}/parking.sqlite",
        tick=False,
    )


@contextlib.asynccontextmanager
async def client_for(app, client=("1.2.3.4", 123)):
    transport = httpx.ASGITransport(app=app, client=client)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        yield c


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def sessions(lot):
    with Session(make_engine(f"sqlite:///{lot}/parking.sqlite")) as s:
        return list(s.exec(select(AdminSession)))


async def test_good_password_gives_a_7_day_session_token(lot, clock):
    async with client_for(make_app(lot, clock)) as c:
        r = await c.post(
            "/api/admin/login", json={"password": PASSWORD}, headers={"User-Agent": "phone"}
        )
        assert r.status_code == 200
        token = r.json()["token"]
        assert len(token) >= 43  # 32 random bytes, base64url
        assert r.json()["expires_at"] == "2026-10-16T12:00:00.000Z"

        r = await c.get("/api/admin/session", headers=bearer(token))
        assert r.status_code == 200
        assert r.json() == {"actor": "session:1", "expires_at": "2026-10-16T12:00:00.000Z"}

    [row] = sessions(lot)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()  # never the token
    assert token not in row.token_hash
    assert (row.ip, row.user_agent, row.revoked_at) == ("1.2.3.4", "phone", None)
    assert row.created_at == T0


@pytest.mark.parametrize("password", ["wrong password", PASSWORD.upper(), PASSWORD + " "])
async def test_bad_password_is_401_and_creates_no_session(lot, clock, password):
    async with client_for(make_app(lot, clock)) as c:
        r = await c.post("/api/admin/login", json={"password": password})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"
    assert sessions(lot) == []


@pytest.mark.parametrize(
    "body", [{}, {"password": ""}, {"password": "x" * 1025}, {"password": PASSWORD, "x": 1}]
)
async def test_login_body_is_validated(lot, clock, body):
    async with client_for(make_app(lot, clock)) as c:
        r = await c.post("/api/admin/login", json=body)
        assert r.status_code == 422


async def test_login_without_a_password_hash_is_503(lot, clock):
    async with client_for(make_app(lot, clock, admin_password_hash=None)) as c:
        r = await c.post("/api/admin/login", json={"password": PASSWORD})
        assert r.status_code == 503


async def test_malformed_password_hash_never_logs_in(lot, clock):
    assert not verify_password("not-an-argon2-hash", PASSWORD)
    async with client_for(make_app(lot, clock, admin_password_hash="$argon2id$broken")) as c:
        r = await c.post("/api/admin/login", json={"password": PASSWORD})
        assert r.status_code == 401


async def test_login_is_limited_to_5_attempts_per_15_minutes_per_ip(lot, clock):
    app = make_app(lot, clock)
    async with client_for(app) as c:
        for _ in range(5):
            r = await c.post("/api/admin/login", json={"password": "nope"})
            assert r.status_code == 401
        r = await c.post("/api/admin/login", json={"password": PASSWORD})  # even the right one
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "rate_limited"
        assert 0 < int(r.headers["Retry-After"]) <= 15 * 60
    async with client_for(app, client=("5.6.7.8", 1)) as other:  # another IP is unaffected
        r = await other.post("/api/admin/login", json={"password": PASSWORD})
        assert r.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        bearer("not-a-session"),
        bearer(""),
        {"Authorization": f"Basic {STATIC}"},
        {"Authorization": STATIC},
    ],
)
async def test_admin_routes_need_a_token(lot, clock, headers):
    async with client_for(make_app(lot, clock)) as c:
        r = await c.get("/api/admin/session", headers=headers)
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == "Bearer"
        r = await c.post("/api/admin/logout", headers=headers)
        assert r.status_code == 401


async def test_static_admin_token_works(lot, clock):
    async with client_for(make_app(lot, clock)) as c:
        r = await c.get("/api/admin/session", headers=bearer(STATIC))
        assert r.json() == {"actor": "admin-token", "expires_at": None}
        r = await c.post("/api/admin/logout", headers=bearer(STATIC))  # nothing to revoke
        assert r.status_code == 204
        r = await c.get("/api/admin/session", headers=bearer(STATIC))
        assert r.status_code == 200


async def test_without_admin_token_only_sessions_work(lot, clock):
    async with client_for(make_app(lot, clock, admin_token=None)) as c:
        assert (await c.get("/api/admin/session", headers=bearer(STATIC))).status_code == 401
        token = (await c.post("/api/admin/login", json={"password": PASSWORD})).json()["token"]
        assert (await c.get("/api/admin/session", headers=bearer(token))).status_code == 200


async def test_session_token_expires_after_7_days(lot, clock):
    async with client_for(make_app(lot, clock)) as c:
        token = (await c.post("/api/admin/login", json={"password": PASSWORD})).json()["token"]
        clock.advance(timedelta(days=7).total_seconds() - 1)
        assert (await c.get("/api/admin/session", headers=bearer(token))).status_code == 200
        clock.advance(1)
        r = await c.get("/api/admin/session", headers=bearer(token))
        assert r.status_code == 401


async def test_logout_revokes_only_that_session(lot, clock):
    async with client_for(make_app(lot, clock)) as c:
        one = (await c.post("/api/admin/login", json={"password": PASSWORD})).json()["token"]
        two = (await c.post("/api/admin/login", json={"password": PASSWORD})).json()["token"]
        assert one != two
        clock.advance(60)
        assert (await c.post("/api/admin/logout", headers=bearer(one))).status_code == 204
        assert (await c.get("/api/admin/session", headers=bearer(one))).status_code == 401
        assert (await c.post("/api/admin/logout", headers=bearer(one))).status_code == 401
        assert (await c.get("/api/admin/session", headers=bearer(two))).status_code == 200
    revoked = {row.id: row.revoked_at for row in sessions(lot)}
    assert revoked == {1: T0 + timedelta(seconds=60), 2: None}


async def test_sessions_survive_an_api_restart(lot, clock):
    async with client_for(make_app(lot, clock)) as c:
        token = (await c.post("/api/admin/login", json={"password": PASSWORD})).json()["token"]
    async with client_for(make_app(lot, clock)) as c:
        assert (await c.get("/api/admin/session", headers=bearer(token))).status_code == 200


def test_cli_hash_password_prints_a_verifiable_env_line():
    result = CliRunner().invoke(cli, ["admin", "hash-password"], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    line = next(x for x in result.stdout.splitlines() if x.startswith("ADMIN_PASSWORD_HASH="))
    value = line.removeprefix("ADMIN_PASSWORD_HASH=")
    assert value[0] == value[-1] == "'"
    assert value[1:-1].startswith("$argon2id$")
    assert verify_password(value[1:-1], PASSWORD)
    assert PASSWORD not in result.stdout


def test_hash_line_reads_back_from_env_file(tmp_path):
    hashed = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    (tmp_path / ".env").write_text(f"ADMIN_PASSWORD_HASH='{hashed}'\n")
    settings = Settings(_env_file=tmp_path / ".env")
    assert settings.admin_password_hash.get_secret_value() == hashed


@pytest.mark.parametrize(
    "answers, error",
    [("short\nshort\n", "at least 10"), (f"{PASSWORD}\nother password\n", "")],
)
def test_cli_hash_password_rejects_short_or_mismatched(answers, error):
    result = CliRunner().invoke(cli, ["admin", "hash-password"], input=answers)
    assert "ADMIN_PASSWORD_HASH" not in result.stdout
    assert error in result.output
