"""Database connections used by the experiment program."""
from __future__ import annotations

from typing import Dict, Optional

import psycopg2

from .config import DbConfig

MOLQO_OFF = "-c enable_molqo=off"


def connect(cfg: DbConfig, application_name: str, molqo_off: bool = True,
            extra_options: str = ""):
    """Open an autocommit connection.

    With molqo_off the learned optimizer is switched off for this session as a
    start-up option, which also survives RESET ALL. Every connection except the
    JOB clients needs this: once enable_molqo is on for the whole server, each
    SELECT of a session without the option is sent to the optimizer service,
    including the point lookups of the YCSB connection.
    """
    kwargs = dict(host=cfg.host, port=cfg.port, dbname=cfg.dbname, user=cfg.user,
                  application_name=application_name)
    if cfg.password:
        kwargs["password"] = cfg.password
    options = " ".join(x for x in ((MOLQO_OFF if molqo_off else ""), extra_options) if x)

    try:
        conn = psycopg2.connect(options=options, **kwargs) if options else psycopg2.connect(**kwargs)
    except psycopg2.OperationalError as e:
        if not (molqo_off and "enable_molqo" in str(e)):
            raise
        # nr_molqo is not preloaded: the setting does not exist, nothing to switch off
        conn = (psycopg2.connect(options=extra_options, **kwargs) if extra_options
                else psycopg2.connect(**kwargs))
    conn.autocommit = True
    return conn


class Admin:
    """Control connection of the Global Agent."""

    def __init__(self, cfg: DbConfig):
        self.cfg = cfg
        self.conn = connect(cfg, "ga_admin")

    def execute(self, sql: str, params=None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def fetchall(self, sql: str, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def fetchone(self, sql: str, params=None):
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def set_server_settings(self, settings: Dict[str, str]) -> None:
        """ALTER SYSTEM + reload, so that every session sees the new values."""
        for name, value in settings.items():
            # names come from gaproto.actions, values are keywords
            self.execute(f"ALTER SYSTEM SET {name} = %s", (value,))
        self.execute("SELECT pg_reload_conf()")

    def reset_server_settings(self, names) -> None:
        for name in names:
            self.execute(f"ALTER SYSTEM RESET {name}")
        self.execute("SELECT pg_reload_conf()")

    def current_setting(self, name: str) -> Optional[str]:
        row = self.fetchone("SELECT setting FROM pg_settings WHERE name = %s", (name,))
        return row[0] if row else None

    def file_setting(self, name: str) -> Optional[str]:
        """Value that sessions without an own SET get after the last reload."""
        row = self.fetchone(
            "SELECT setting FROM pg_file_settings WHERE name = %s AND applied "
            "ORDER BY seqno DESC LIMIT 1", (name,))
        return row[0] if row else None

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
