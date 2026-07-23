"""Adapter unit tests. Network-free: the cloud adapter is tested against a mocked API."""

import io
import json
import urllib.error
from unittest import mock

import pytest

from instances.adapters import (
    AdapterError,
    PostgresAdapter,
    SQLiteAdapter,
    SupabaseCloudAdapter,
    _check_identifier,
    get_adapter,
)

VALID_REF = "abcdefghijklmnopqrst"


class TestIdentifierCheck:
    def test_accepts_plain_identifiers(self):
        _check_identifier("tickets")
        _check_identifier("_private")
        _check_identifier("Table2")

    @pytest.mark.parametrize("bad", ["", "1table", "users; drop", 'x"y', "a-b"])
    def test_rejects_invalid_identifiers(self, bad):
        with pytest.raises(AdapterError):
            _check_identifier(bad)


class TestGetAdapter:
    def test_returns_the_right_class(self):
        assert isinstance(get_adapter("sqlite", ":memory:"), SQLiteAdapter)
        assert isinstance(get_adapter("postgres", "postgresql://x"), PostgresAdapter)
        assert isinstance(get_adapter("supabase", VALID_REF), SupabaseCloudAdapter)

    def test_unknown_type_raises(self):
        with pytest.raises(AdapterError, match="Unknown adapter type"):
            get_adapter("mysql", "whatever")


class TestSQLiteAdapter:
    @pytest.fixture
    def adapter(self, tmp_path):
        return SQLiteAdapter(str(tmp_path / "test.db"))

    def test_full_cycle(self, adapter):
        ddl = adapter.execute_sql("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        assert "rows_affected" in ddl

        adapter.execute_sql("INSERT INTO recipes (title) VALUES ('carbonara')")
        out = adapter.execute_sql("SELECT * FROM recipes")
        assert out["columns"] == ["id", "title"]
        assert out["rows"] == [{"id": 1, "title": "carbonara"}]

        assert adapter.list_tables() == ["recipes"]

        cols = adapter.describe_table("recipes")
        assert cols[0]["name"] == "id"
        assert cols[0]["primary_key"] is True
        assert cols[1] == {
            "name": "title",
            "type": "TEXT",
            "nullable": False,
            "primary_key": False,
            "default": None,
            "references": None,
        }

        assert adapter.get_schema() == {"recipes": cols}

    def test_describe_missing_table_raises(self, adapter):
        adapter.execute_sql("CREATE TABLE x (id INTEGER)")
        with pytest.raises(AdapterError, match="does not exist"):
            adapter.describe_table("nope")


def _fake_response(payload):
    return mock.MagicMock(
        __enter__=lambda s: mock.MagicMock(read=lambda: json.dumps(payload).encode()),
        __exit__=lambda s, *a: False,
    )


class TestSupabaseCloudAdapter:
    def test_invalid_ref_raises(self):
        with pytest.raises(AdapterError, match="Invalid project ref"):
            SupabaseCloudAdapter("not-a-ref").list_tables()

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
        with pytest.raises(AdapterError, match="SUPABASE_ACCESS_TOKEN"):
            SupabaseCloudAdapter(VALID_REF).list_tables()

    def test_list_tables_parses_rows(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        payload = [{"table_name": "tickets"}, {"table_name": "users"}]
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)) as opened:
            assert SupabaseCloudAdapter(VALID_REF).list_tables() == ["tickets", "users"]
        req = opened.call_args[0][0]
        assert req.full_url.endswith(f"/projects/{VALID_REF}/database/query")
        assert req.headers["Authorization"] == "Bearer sbp_test"

    def test_execute_sql_wraps_rows_and_truncates(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        payload = [{"n": i} for i in range(80)]
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            out = SupabaseCloudAdapter(VALID_REF).execute_sql("SELECT n FROM big")
        assert out["columns"] == ["n"]
        assert len(out["rows"]) == 50

    def test_execute_sql_empty_result(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        with mock.patch("urllib.request.urlopen", return_value=_fake_response([])):
            assert SupabaseCloudAdapter(VALID_REF).execute_sql("CREATE TABLE t (id int)") == {
                "ok": True,
                "rows": [],
            }

    def test_api_error_becomes_adapter_error(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        err = urllib.error.HTTPError(
            url="x", code=401, msg="unauthorized", hdrs=None, fp=io.BytesIO(b'{"message": "bad token"}')
        )
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(AdapterError, match="Supabase API 401: bad token"):
                SupabaseCloudAdapter(VALID_REF).list_tables()


class TestSQLiteForeignKeys:
    def test_fk_appears_on_referencing_column(self, tmp_path):
        a = SQLiteAdapter(str(tmp_path / "fk.db"))
        a.execute_sql("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT)")
        a.execute_sql(
            "CREATE TABLE reviews (id INTEGER PRIMARY KEY, "
            "recipe_id INTEGER NOT NULL REFERENCES recipes(id), rating INTEGER)"
        )
        cols = {c["name"]: c for c in a.describe_table("reviews")}
        assert cols["recipe_id"]["references"] == {"table": "recipes", "column": "id"}
        assert cols["rating"]["references"] is None


class TestSupabaseBatchedSchema:
    def test_whole_schema_in_four_api_calls(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        a = SupabaseCloudAdapter(VALID_REF)
        responses = [
            [{"table_name": "recipes"}, {"table_name": "reviews"}],
            [
                {
                    "table_name": "recipes",
                    "column_name": "id",
                    "data_type": "integer",
                    "is_nullable": "NO",
                    "column_default": None,
                },
                {
                    "table_name": "reviews",
                    "column_name": "id",
                    "data_type": "integer",
                    "is_nullable": "NO",
                    "column_default": None,
                },
                {
                    "table_name": "reviews",
                    "column_name": "recipe_id",
                    "data_type": "integer",
                    "is_nullable": "NO",
                    "column_default": None,
                },
            ],
            [{"table_name": "recipes", "column_name": "id"}, {"table_name": "reviews", "column_name": "id"}],
            [
                {
                    "table_name": "reviews",
                    "column_name": "recipe_id",
                    "ref_table": "recipes",
                    "ref_column": "id",
                }
            ],
        ]
        with mock.patch.object(a, "_query", side_effect=responses) as q:
            schema = a.get_schema()
        assert q.call_count == 4  # NOT 1 + 3 per table
        assert set(schema) == {"recipes", "reviews"}
        by_name = {c["name"]: c for c in schema["reviews"]}
        assert by_name["id"]["primary_key"] is True
        assert by_name["recipe_id"]["references"] == {"table": "recipes", "column": "id"}
        assert schema["recipes"][0]["references"] is None

    def test_429_is_retried_with_backoff(self, monkeypatch):
        import io
        import urllib.error

        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        a = SupabaseCloudAdapter(VALID_REF)
        throttle = urllib.error.HTTPError(
            url="x",
            code=429,
            msg="too many",
            hdrs={"Retry-After": "0"},
            fp=io.BytesIO(b'{"message": "ThrottlerException"}'),
        )
        ok = mock.MagicMock(
            __enter__=lambda s: mock.MagicMock(read=lambda: b'[{"table_name": "t"}]'),
            __exit__=lambda s, *args: False,
        )
        with (
            mock.patch("urllib.request.urlopen", side_effect=[throttle, ok]),
            mock.patch("time.sleep") as slept,
        ):
            assert a.list_tables() == ["t"]
        slept.assert_called_once()

    def test_429_gives_up_after_retries(self, monkeypatch):
        import io
        import urllib.error

        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        a = SupabaseCloudAdapter(VALID_REF)

        def make_429():
            return urllib.error.HTTPError(
                url="x",
                code=429,
                msg="too many",
                hdrs={},
                fp=io.BytesIO(b'{"message": "ThrottlerException"}'),
            )

        with (
            mock.patch("urllib.request.urlopen", side_effect=[make_429(), make_429(), make_429()]),
            mock.patch("time.sleep"),
        ):
            with pytest.raises(AdapterError, match="429"):
                a.list_tables()


class TestQuerySql:
    """query_sql: the read-only path — writes rejected by the ENGINE."""

    @pytest.fixture
    def adapter(self, tmp_path):
        a = SQLiteAdapter(str(tmp_path / "q.db"))
        a.execute_sql("CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT)")
        a.execute_sql("INSERT INTO recipes (title) VALUES ('carbonara')")
        return a

    def test_select_returns_rows(self, adapter):
        out = adapter.query_sql("SELECT title FROM recipes")
        assert out["rows"] == [{"title": "carbonara"}]

    def test_trailing_semicolon_tolerated(self, adapter):
        assert adapter.query_sql("SELECT 1 AS x;")["rows"] == [{"x": 1}]

    def test_write_rejected_by_the_engine(self, adapter):
        with pytest.raises(AdapterError, match="Read-only query failed"):
            adapter.query_sql("DELETE FROM recipes")
        # nothing was deleted
        assert adapter.execute_sql("SELECT count(*) AS n FROM recipes")["rows"] == [{"n": 1}]

    def test_multiple_statements_rejected(self, adapter):
        with pytest.raises(AdapterError, match="exactly one statement"):
            adapter.query_sql("SELECT 1; DELETE FROM recipes")

    def test_empty_query_rejected(self, adapter):
        with pytest.raises(AdapterError, match="Empty query"):
            adapter.query_sql("  ; ")

    def test_supabase_wraps_in_read_only_transaction(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        a = SupabaseCloudAdapter(VALID_REF)
        with mock.patch.object(a, "_query", return_value=[{"policyname": "p1"}]) as q:
            out = a.query_sql("SELECT policyname FROM pg_policies")
        assert q.call_args.args == (
            "begin transaction read only; SELECT policyname FROM pg_policies; rollback",
        )
        assert out["rows"] == [{"policyname": "p1"}]


class TestSupabaseFunctions:
    """Edge functions via the Management API (capability "functions")."""

    def _adapter(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        return SupabaseCloudAdapter(VALID_REF)

    def test_capability_declared_only_on_supabase(self):
        from instances.adapters import PostgresAdapter

        assert "functions" in SupabaseCloudAdapter.capabilities
        assert "functions" not in SQLiteAdapter.capabilities
        assert "functions" not in PostgresAdapter.capabilities

    def test_list_functions_normalizes_rows(self, monkeypatch):
        a = self._adapter(monkeypatch)
        rows = [
            {
                "slug": "hello",
                "name": "hello",
                "status": "ACTIVE",
                "version": 3,
                "verify_jwt": True,
                "updated_at": "2026-07-22",
                "id": "x",
                "extra": 1,
            }
        ]
        with mock.patch.object(a, "_api", return_value=rows) as api:
            out = a.list_functions()
        assert api.call_args.args == ("GET", "/functions")
        assert out == [
            {
                "slug": "hello",
                "name": "hello",
                "status": "ACTIVE",
                "version": 3,
                "verify_jwt": True,
                "updated_at": "2026-07-22",
            }
        ]

    def test_get_function_body_returns_source(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_api", return_value="Deno.serve(() => new Response('hi'))"):
            assert "Deno.serve" in a.get_function_body("hello")

    def test_get_function_body_rejects_bundles(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_api", return_value="ESZP\x00\x01binary"):
            with pytest.raises(AdapterError, match="bundle"):
                a.get_function_body("hello")

    def test_deploy_uses_the_bundle_endpoint_with_multipart_source(self, monkeypatch):
        """The dashboard/CLI-compatible deploy path: multipart source in,
        eszip built server-side, artifact in the response ignored."""
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_api", return_value={"version": 7, "status": "ACTIVE"}) as api:
            out = a.deploy_function("hello", "Deno.serve(() => new Response('hi'))", verify_jwt=False)
        method, path = api.call_args.args
        assert (method, path) == ("POST", "/functions/deploy?slug=hello")
        form = api.call_args.kwargs["data"].decode()
        assert 'name="metadata"' in form and '"verify_jwt": false' in form
        assert 'filename="index.ts"' in form and "Deno.serve" in form
        assert api.call_args.kwargs["content_type"].startswith("multipart/form-data; boundary=")
        assert out == {"slug": "hello", "version": 7, "status": "ACTIVE"}

    def test_slug_validation(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with pytest.raises(AdapterError, match="Invalid function slug"):
            a.get_function_body("../../etc")

    def test_delete_function(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_api", return_value=None) as api:
            out = a.delete_function("hello")
        assert api.call_args.args == ("DELETE", "/functions/hello")
        assert out == {"slug": "hello", "deleted": True}


class TestSupabaseAdvisors:
    """Advisor lints (capability "advisors"): the dashboard's Security /
    Performance Advisor reports, normalized for the agent."""

    def _adapter(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        return SupabaseCloudAdapter(VALID_REF)

    def test_capability_declared_only_on_supabase(self):
        assert "advisors" in SupabaseCloudAdapter.capabilities
        assert "advisors" not in SQLiteAdapter.capabilities
        assert "advisors" not in PostgresAdapter.capabilities

    def test_get_advisors_normalizes_lints(self, monkeypatch):
        a = self._adapter(monkeypatch)
        payload = {
            "lints": [
                {
                    "name": "rls_disabled_in_public",
                    "title": "RLS Disabled in Public",
                    "level": "ERROR",
                    "facing": "EXTERNAL",
                    "categories": ["SECURITY"],
                    "description": "RLS has not been enabled",
                    "detail": "Table `public.reviews` is public, but RLS has not been enabled.",
                    "remediation": "https://supabase.com/docs/guides/database/database-linter",
                    "metadata": {"schema": "public", "name": "reviews", "type": "table"},
                    "cache_key": "x",
                }
            ]
        }
        with mock.patch.object(a, "_api", return_value=payload) as api:
            out = a.get_advisors("security")
        assert api.call_args.args == ("GET", "/advisors/security")
        assert out == [
            {
                "name": "rls_disabled_in_public",
                "title": "RLS Disabled in Public",
                "level": "ERROR",
                "description": "RLS has not been enabled",
                "detail": "Table `public.reviews` is public, but RLS has not been enabled.",
                "remediation": "https://supabase.com/docs/guides/database/database-linter",
                "metadata": {"schema": "public", "name": "reviews", "type": "table"},
            }
        ]

    def test_performance_kind_hits_its_endpoint(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_api", return_value={"lints": []}) as api:
            assert a.get_advisors("performance") == []
        assert api.call_args.args == ("GET", "/advisors/performance")

    def test_unknown_kind_raises_without_calling_the_api(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_api") as api:
            with pytest.raises(AdapterError, match="Advisor kind"):
                a.get_advisors("vibes")
        api.assert_not_called()


class TestSupabaseStorage:
    """Buckets (capability "storage"): the Management API only lists them;
    mutations are SQL on storage.buckets — the engine (FK from
    storage.objects) is what refuses deleting a non-empty bucket."""

    def _adapter(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        return SupabaseCloudAdapter(VALID_REF)

    def test_capability_declared_only_on_supabase(self):
        from instances.adapters import PostgresAdapter

        assert "storage" in SupabaseCloudAdapter.capabilities
        assert "storage" not in SQLiteAdapter.capabilities
        assert "storage" not in PostgresAdapter.capabilities

    def test_list_buckets_normalizes_rows(self, monkeypatch):
        a = self._adapter(monkeypatch)
        rows = [
            {
                "id": "avatars",
                "name": "avatars",
                "public": True,
                "owner": "x",
                "created_at": "c",
                "updated_at": "u",
            }
        ]
        with mock.patch.object(a, "_api", return_value=rows) as api:
            out = a.list_buckets()
        assert api.call_args.args == ("GET", "/storage/buckets")
        assert out == [
            {"id": "avatars", "name": "avatars", "public": True, "created_at": "c", "updated_at": "u"}
        ]

    def test_create_bucket_inserts_with_options(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_query", return_value=[]) as q:
            out = a.create_bucket(
                "avatars", public=True, file_size_limit=1048576, allowed_mime_types=["image/png", "image/*"]
            )
        sql = q.call_args.args[0]
        assert "insert into storage.buckets" in sql
        assert "'avatars', 'avatars', true, 1048576, ARRAY['image/png', 'image/*']" in sql
        assert out == {"name": "avatars", "public": True, "created": True}

    def test_create_bucket_defaults_private_no_limits(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_query", return_value=[]) as q:
            a.create_bucket("docs")
        assert "'docs', 'docs', false, NULL, NULL" in q.call_args.args[0]

    def test_bucket_name_validation_guards_sql(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with pytest.raises(AdapterError, match="Invalid bucket name"):
            a.create_bucket("x'; drop table storage.buckets; --")
        with pytest.raises(AdapterError, match="Invalid MIME type"):
            a.create_bucket("ok", allowed_mime_types=["image/png'); --"])

    def test_update_bucket_patches_only_what_was_passed(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_query", return_value=[]) as q:
            out = a.update_bucket("avatars", public=False)
        sql = q.call_args.args[0]
        assert sql == "update storage.buckets set public = false where id = 'avatars'"
        assert out == {"name": "avatars", "updated": ["public"]}

    def test_update_bucket_clears_limits_with_zero_and_empty(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_query", return_value=[]) as q:
            a.update_bucket("avatars", file_size_limit=0, allowed_mime_types=[])
        sql = q.call_args.args[0]
        assert "file_size_limit = NULL" in sql and "allowed_mime_types = NULL" in sql

    def test_update_bucket_requires_a_change(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with pytest.raises(AdapterError, match="Nothing to update"):
            a.update_bucket("avatars")

    def test_delete_bucket(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_query", return_value=[]) as q:
            out = a.delete_bucket("avatars")
        assert q.call_args.args[0] == "delete from storage.buckets where id = 'avatars'"
        assert out == {"name": "avatars", "deleted": True}

    def test_delete_non_empty_bucket_gets_a_readable_error(self, monkeypatch):
        a = self._adapter(monkeypatch)
        fk = AdapterError('Supabase API 400: violates foreign key constraint "objects_bucketId_fkey"')
        with mock.patch.object(a, "_query", side_effect=fk):
            with pytest.raises(AdapterError, match="not empty"):
                a.delete_bucket("avatars")


class TestSupabaseAuthConfig:
    """Auth config (capability "auth_config"): secrets are masked AT THE
    SOURCE — no consumer (model, audit, GUI) ever sees a credential, and
    none can be written through Diabase."""

    def _adapter(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test")
        return SupabaseCloudAdapter(VALID_REF)

    def test_capability_declared_only_on_supabase(self):
        from instances.adapters import PostgresAdapter

        assert "auth_config" in SupabaseCloudAdapter.capabilities
        assert "auth_config" not in SQLiteAdapter.capabilities
        assert "auth_config" not in PostgresAdapter.capabilities

    def test_get_auth_config_redacts_set_secrets_and_keeps_unset_ones(self, monkeypatch):
        a = self._adapter(monkeypatch)
        live = {  # nosec — fake credentials exercising the redaction
            "site_url": "https://cucina.it",
            "smtp_pass": "hunter2",  # nosec B105
            "external_github_secret": "gh_secret",  # nosec B105
            "external_google_secret": "",  # nosec B105 — unset stays visibly unset
            "sms_twilio_auth_token": "tok",  # nosec B105
            "password_min_length": 8,
        }
        with mock.patch.object(a, "_api", return_value=live) as api:
            out = a.get_auth_config()
        assert api.call_args.args == ("GET", "/config/auth")
        assert out["site_url"] == "https://cucina.it" and out["password_min_length"] == 8
        from instances.adapters import AUTH_SECRET_MASK

        assert out["smtp_pass"] == AUTH_SECRET_MASK
        assert out["external_github_secret"] == AUTH_SECRET_MASK
        assert out["sms_twilio_auth_token"] == AUTH_SECRET_MASK
        assert out["external_google_secret"] == ""

    def test_update_patches_only_the_passed_keys(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_api", return_value={}) as api:
            out = a.update_auth_config({"disable_signup": True, "site_url": "https://x"})
        method, path = api.call_args.args[:2]
        assert (method, path) == ("PATCH", "/config/auth")
        assert api.call_args.args[2] == {"disable_signup": True, "site_url": "https://x"}
        assert out == {"updated": ["disable_signup", "site_url"]}

    def test_update_refuses_secret_keys(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with mock.patch.object(a, "_api") as api:
            with pytest.raises(AdapterError, match="secret keys"):
                a.update_auth_config({"smtp_pass": "new", "site_url": "https://x"})  # nosec B105
        api.assert_not_called()  # nothing reached the API

    def test_update_requires_changes(self, monkeypatch):
        a = self._adapter(monkeypatch)
        with pytest.raises(AdapterError, match="non-empty"):
            a.update_auth_config({})
