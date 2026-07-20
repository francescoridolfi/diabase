"""Adapters towards the Supabase instances Diabase manages.

Every connected instance gets an adapter exposing the same interface
(list_tables / describe_table / execute_sql): the agent only ever talks
to this interface and doesn't know which deployment it is operating on.
Current implementations: SQLite (local hacking), Postgres (self-hosted),
Supabase Cloud (Management API).
"""

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request


class AdapterError(Exception):
    pass


class BaseAdapter:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def list_tables(self):
        raise NotImplementedError

    def describe_table(self, table: str):
        raise NotImplementedError

    def execute_sql(self, sql: str):
        raise NotImplementedError

    def get_schema(self):
        """Full schema as {table: [columns]} for the schema view."""
        return {t: self.describe_table(t) for t in self.list_tables()}


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_identifier(name: str):
    if not _IDENT_RE.match(name):
        raise AdapterError(f"Invalid table name: {name!r}")


class SQLiteAdapter(BaseAdapter):
    def _connect(self):
        conn = sqlite3.connect(self.dsn, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def list_tables(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [r["name"] for r in rows]

    def describe_table(self, table: str):
        _check_identifier(table)
        with self._connect() as conn:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            fks = {
                r["from"]: {"table": r["table"], "column": r["to"]}
                for r in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            }
        if not rows:
            raise AdapterError(f"Table {table!r} does not exist")
        return [
            {
                "name": r["name"],
                "type": r["type"] or "ANY",
                "nullable": not r["notnull"],
                "primary_key": bool(r["pk"]),
                "default": r["dflt_value"],
                "references": fks.get(r["name"]),
            }
            for r in rows
        ]

    def execute_sql(self, sql: str):
        with self._connect() as conn:
            cur = conn.execute(sql)
            if cur.description:  # query returning rows
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchmany(50)]
                return {"columns": cols, "rows": rows, "truncated_at": 50}
            conn.commit()
            return {"rows_affected": cur.rowcount}


_FK_SQL = (
    "SELECT kcu.column_name, ccu.table_name, ccu.column_name "
    "FROM information_schema.table_constraints tc "
    "JOIN information_schema.key_column_usage kcu "
    "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
    "JOIN information_schema.constraint_column_usage ccu "
    "ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
    "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public' AND tc.table_name = %s"
)


class PostgresAdapter(BaseAdapter):
    def _connect(self):
        try:
            import psycopg2
        except ImportError:
            raise AdapterError(
                "psycopg2 is not installed: `pip install psycopg2-binary` to connect Postgres instances"
            ) from None
        return psycopg2.connect(self.dsn, connect_timeout=5)

    def list_tables(self):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
            )
            return [r[0] for r in cur.fetchall()]

    def describe_table(self, table: str):
        _check_identifier(table)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                (table,),
            )
            rows = cur.fetchall()
            if not rows:
                raise AdapterError(f"Table {table!r} does not exist")
            cur.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                "WHERE i.indrelid=%s::regclass AND i.indisprimary",
                (table,),
            )
            pks = {r[0] for r in cur.fetchall()}
            cur.execute(_FK_SQL, (table,))
            fks = {col: {"table": rt, "column": rc} for col, rt, rc in cur.fetchall()}
        return [
            {
                "name": name,
                "type": dtype,
                "nullable": nullable == "YES",
                "primary_key": name in pks,
                "default": default,
                "references": fks.get(name),
            }
            for name, dtype, nullable, default in rows
        ]

    def execute_sql(self, sql: str):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            if cur.description:
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchmany(50)]
                conn.commit()
                return {
                    "columns": cols,
                    "rows": [{k: str(v) for k, v in r.items()} for r in rows],
                    "truncated_at": 50,
                }
            conn.commit()
            return {"rows_affected": cur.rowcount}


class SupabaseCloudAdapter(BaseAdapter):
    """Supabase Cloud instance via the Management API.

    The dsn is the project ref (e.g. "abcdefghijklmnopqrst"); authentication
    uses the Personal Access Token in SUPABASE_ACCESS_TOKEN. No Postgres
    credentials involved: everything goes through api.supabase.com.
    """

    API = "https://api.supabase.com/v1"

    @property
    def ref(self):
        ref = self.dsn.strip()
        if not re.match(r"^[a-z]{20}$", ref):
            raise AdapterError(
                f"Invalid project ref: {ref!r} (expected the 20-letter ref from Project Settings → General)"
            )
        return ref

    def _query(self, sql: str):
        ref = self.ref  # validates the project ref before anything else
        token = os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()
        if not token:
            raise AdapterError(
                "SUPABASE_ACCESS_TOKEN is not set: a Supabase Personal Access Token is required"
            )
        req = urllib.request.Request(  # noqa: S310 — fixed https host
            f"{self.API}/projects/{ref}/database/query",
            data=json.dumps({"query": sql}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                # Cloudflare in front of api.supabase.com rejects urllib's default UA (error 1010)
                "User-Agent": "diabase/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 # nosec B310 — fixed https host
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise AdapterError(f"Supabase API {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise AdapterError(f"Supabase API unreachable: {e.reason}") from None
        result = json.loads(body) if body else []
        if isinstance(result, dict):  # some API versions wrap results in {"result": [...]}
            result = result.get("result", result)
        return result if isinstance(result, list) else []

    def list_tables(self):
        rows = self._query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
        )
        return [r["table_name"] for r in rows]

    def describe_table(self, table: str):
        _check_identifier(table)
        rows = self._query(
            "SELECT column_name, data_type, is_nullable, column_default "  # noqa: S608 # nosec B608 — identifier validated above
            "FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{table}' ORDER BY ordinal_position"
        )
        if not rows:
            raise AdapterError(f"Table {table!r} does not exist")
        pks = {
            r["attname"]
            for r in self._query(
                "SELECT a.attname FROM pg_index i "  # noqa: S608 # nosec B608 — identifier validated above
                "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                f"WHERE i.indrelid='public.{table}'::regclass AND i.indisprimary"
            )
        }
        fks = {
            r["column_name"]: {"table": r["ref_table"], "column": r["ref_column"]}
            for r in self._query(
                "SELECT kcu.column_name, ccu.table_name AS ref_table, ccu.column_name AS ref_column "  # noqa: S608 # nosec B608 — identifier validated above
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
                "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public' "
                f"AND tc.table_name = '{table}'"
            )
        }
        return [
            {
                "name": r["column_name"],
                "type": r["data_type"],
                "nullable": r["is_nullable"] == "YES",
                "primary_key": r["column_name"] in pks,
                "default": r["column_default"],
                "references": fks.get(r["column_name"]),
            }
            for r in rows
        ]

    def execute_sql(self, sql: str):
        rows = self._query(sql)
        if rows:
            cols = list(rows[0].keys())
            return {
                "columns": cols,
                "rows": [{k: str(v) for k, v in r.items()} for r in rows[:50]],
                "truncated_at": 50,
            }
        return {"ok": True, "rows": []}


ADAPTERS = {
    "sqlite": SQLiteAdapter,
    "postgres": PostgresAdapter,
    "supabase": SupabaseCloudAdapter,
}


def get_adapter(adapter_type: str, dsn: str) -> BaseAdapter:
    try:
        cls = ADAPTERS[adapter_type]
    except KeyError:
        raise AdapterError(f"Unknown adapter type: {adapter_type!r}") from None
    return cls(dsn)
