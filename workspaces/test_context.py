"""Context tests: hybrid inline/index strategy, determinism, audited services."""

import pytest

from agents.policy import AutonomyPolicy
from agents.prompts import build_system_prompt
from agents.tools import BoundToolset
from audit.models import AuditEntry
from audit.services import AuditedAdapter
from instances.adapters import SQLiteAdapter
from instances.models import Server
from workspaces.context import INLINE_MAX_BYTES, TOTAL_INLINE_BUDGET, build_context_block
from workspaces.models import ContextFile, Project
from workspaces.services import (
    ContextFileTooLarge,
    delete_context_file,
    save_context_file,
    set_system_prompt,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(tmp_path):
    server = Server.objects.create(name="S", adapter_type="sqlite", dsn=str(tmp_path / "t.db"))
    return Project.objects.create(name="P", server=server)


class TestContextBlock:
    def test_empty_without_files(self, project):
        assert build_context_block(project) == ""

    def test_small_files_are_inlined(self, project):
        save_context_file(project, "conventions.md", "# Conventions\nAlways use uuid PKs.")
        block = build_context_block(project)
        assert "Always use uuid PKs." in block

    def test_large_files_are_indexed_not_inlined(self, project):
        big = "# Big domain doc\n" + ("x" * (INLINE_MAX_BYTES + 100))
        save_context_file(project, "domain.md", big)
        block = build_context_block(project)
        assert "read_context_file" in block
        assert "domain.md" in block
        assert "xxxx" not in block  # body not inlined
        assert "# Big domain doc" in block  # first line as index description

    def test_budget_degrades_small_files_to_index(self, project):
        # each file fits individually, together they exceed the budget
        chunk = "y" * (INLINE_MAX_BYTES - 100)
        n_files = TOTAL_INLINE_BUDGET // INLINE_MAX_BYTES + 2
        for i in range(n_files):
            save_context_file(project, f"f{i:02d}.md", chunk)
        block = build_context_block(project)
        assert "read_context_file" in block  # someone got demoted to the index

    def test_deterministic_across_calls(self, project):
        save_context_file(project, "b.md", "bee")
        save_context_file(project, "a.md", "ay")
        assert build_context_block(project) == build_context_block(project)
        # sorted by name: a before b
        block = build_context_block(project)
        assert block.index("a.md") < block.index("b.md")

    def test_system_prompt_composition(self, project):
        set_system_prompt(project, "Speak Italian to the user.")
        save_context_file(project, "notes.md", "tickets.stato is an enum")
        prompt = build_system_prompt(project)
        assert "# Project instructions" in prompt
        assert "Speak Italian to the user." in prompt
        assert "tickets.stato is an enum" in prompt


class TestReadContextFileTool:
    def make_toolset(self, project):
        adapter = AuditedAdapter(SQLiteAdapter(project.server.dsn), project=project, actor="agent-x")
        return BoundToolset(adapter=adapter, policy=AutonomyPolicy("full"), project=project, actor="agent-x")

    def test_reads_file_and_audits(self, project):
        save_context_file(project, "domain.md", "orders reference customers")
        out = self.make_toolset(project).execute("read_context_file", {"name": "domain.md"})
        assert out["content"] == "orders reference customers"
        entry = AuditEntry.objects.get(action="read_context_file")
        assert entry.actor == "agent-x" and entry.outcome == "success"

    def test_missing_file_lists_available(self, project):
        save_context_file(project, "a.md", "a")
        out = self.make_toolset(project).execute("read_context_file", {"name": "nope.md"})
        assert "error" in out and out["available"] == ["a.md"]

    def test_available_in_read_only_mode(self, project):
        adapter = AuditedAdapter(SQLiteAdapter(project.server.dsn), project=project)
        toolset = BoundToolset(
            adapter=adapter, policy=AutonomyPolicy("read_only"), project=project, actor="a"
        )
        assert "read_context_file" in [s.name for s in toolset.allowed_specs()]


class TestServices:
    def test_prompt_update_is_audited(self, project):
        set_system_prompt(project, "New rules", user="francesco")
        entry = AuditEntry.objects.get(action="project.prompt_updated")
        assert entry.actor == "francesco"
        assert entry.payload_in == {"system_prompt": "New rules"}

    def test_file_lifecycle_is_audited(self, project):
        save_context_file(project, "x.md", "v1", user="francesco")
        save_context_file(project, "x.md", "v2", user="francesco")
        delete_context_file(project, "x.md", user="francesco")
        actions = list(
            AuditEntry.objects.filter(action__startswith="context_file")
            .order_by("created_at")
            .values_list("action", flat=True)
        )
        assert actions == ["context_file.added", "context_file.updated", "context_file.removed"]
        assert not ContextFile.objects.exists()

    def test_size_limit_enforced(self, project):
        with pytest.raises(ContextFileTooLarge):
            save_context_file(project, "huge.md", "z" * (ContextFile.MAX_SIZE + 1))


class TestChunkedReadAndSearch:
    def make_toolset(self, project):
        adapter = AuditedAdapter(SQLiteAdapter(project.server.dsn), project=project, actor="agent-x")
        return BoundToolset(adapter=adapter, policy=AutonomyPolicy("full"), project=project, actor="agent-x")

    def test_read_returns_a_window_with_cursor_info(self, project):
        content = "\n".join(f"line {i}" for i in range(1, 501))
        save_context_file(project, "big.md", content)
        ts = self.make_toolset(project)
        out = ts.execute("read_context_file", {"name": "big.md", "offset": 100, "limit": 50})
        assert out["content"].splitlines()[0] == "line 100"
        assert out["returned_lines"] == 50
        assert out["total_lines"] == 500
        assert out["has_more"] is True
        tail = ts.execute("read_context_file", {"name": "big.md", "offset": 481, "limit": 50})
        assert tail["returned_lines"] == 20
        assert tail["has_more"] is False

    def test_read_defaults_are_bounded(self, project):
        save_context_file(project, "small.md", "just one line")
        out = self.make_toolset(project).execute("read_context_file", {"name": "small.md"})
        assert out["content"] == "just one line"
        assert out["has_more"] is False

    def test_search_finds_matches_across_files(self, project):
        save_context_file(project, "a.md", "# Users\nThe users table stores accounts.\n")
        save_context_file(project, "b.md", "# Payments\nEach payment links a user to a plan.\n")
        ts = self.make_toolset(project)
        out = ts.execute("search_context_files", {"query": "user"})
        files = {m["file"] for m in out["matches"]}
        assert files == {"a.md", "b.md"}
        assert all("line" in m and "text" in m for m in out["matches"])
        assert out["truncated"] is False
        assert AuditEntry.objects.filter(action="search_context_files").exists()

    def test_search_caps_matches(self, project):
        save_context_file(project, "spam.md", "\n".join("needle here" for _ in range(100)))
        out = self.make_toolset(project).execute("search_context_files", {"query": "needle"})
        assert len(out["matches"]) == 40
        assert out["truncated"] is True

    def test_index_outline_lists_headings_with_line_numbers(self, project):
        big = "# Intro\n" + ("filler\n" * 300) + "## Data model\n" + ("filler\n" * 300) + "## API\n"
        save_context_file(project, "domain.md", big)
        block = build_context_block(project)
        assert "# Intro (line 1)" in block
        assert "## Data model (line 302)" in block
        assert "search_context_files" in block
