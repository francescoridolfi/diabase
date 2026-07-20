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
