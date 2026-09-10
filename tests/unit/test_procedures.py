from pathlib import Path
from study_agent.core.context import ContextManager
from study_agent.study.procedures import (
    APPROVED,
    DRAFT,
    format_procedure,
    format_procedures_block,
    load_procedures,
    match_procedures,
    parse_procedure_file,
    procedure_injection_text,
)


def _write(tmp_path: Path, filename: str, proc_name: str, **kwargs) -> Path:
    """Writes a procedure file; status defaults to approved."""
    kwargs.setdefault("status", APPROVED)
    kwargs.setdefault("triggers", ["demo"])
    path = tmp_path / filename
    path.write_text(
        format_procedure(proc_name, "1. do\n2. done", **kwargs),
        encoding="utf-8",
    )
    return path


def test_format_parse_roundtrip(tmp_path: Path):
    """Verify template output parses back with shape intact."""
    path = _write(
        tmp_path,
        "r.md",
        "demo",
        triggers=["ingest", "lecture"],
        scope="When ingesting.",
        rationale="Because listing first.",
        version=3,
    )
    proc = parse_procedure_file(path)
    assert proc is not None
    assert proc.name == "demo"
    assert proc.triggers == ["ingest", "lecture"]
    assert proc.scope == "When ingesting."
    assert proc.rationale == "Because listing first."
    assert proc.version == 3
    assert proc.status == APPROVED


def test_parse_rejects_garbage(tmp_path: Path):
    """Verify missing/unreadable/malformed files yield None, never raise."""
    plain = tmp_path / "plain.md"
    plain.write_text("# no frontmatter", encoding="utf-8")
    assert parse_procedure_file(plain) is None

    bad_yaml = tmp_path / "bad.md"
    bad_yaml.write_text("---\n[unclosed\n---\n## Steps\nx", encoding="utf-8")
    assert parse_procedure_file(bad_yaml) is None

    nameless = tmp_path / "nameless.md"
    nameless.write_text("---\nstatus: approved\n---\n## Steps\nx", encoding="utf-8")
    assert parse_procedure_file(nameless) is None

    assert parse_procedure_file(tmp_path / "missing.md") is None
    assert load_procedures(tmp_path / "no-such-dir") == []


def test_match_statuses_and_caps(tmp_path: Path):
    """Verify approved-only default, draft opt-in, archive exclusion, caps."""
    _write(tmp_path, "a.md", "alpha", triggers=["ingest"])
    _write(tmp_path, "b.md", "beta", triggers=["ingest"], status=DRAFT)
    _write(tmp_path, "c.md", "gamma", triggers=["ingest"], status="archived")
    _write(tmp_path, "d.md", "delta", triggers=["other"])

    approved = match_procedures(tmp_path, "please ingest this")
    assert [p.name for p in approved] == ["alpha"]
    assert all(p.status == APPROVED for p in approved)
    drafts = match_procedures(tmp_path, "please ingest this", statuses=(DRAFT,))
    assert all(p.status == DRAFT for p in drafts)
    assert match_procedures(tmp_path, "unrelated request") == []
    assert match_procedures(tmp_path, "  ") == []
    assert len(match_procedures(tmp_path, "ingest", limit=1)) <= 1
    capped = match_procedures(tmp_path, "ingest", max_chars=10)
    assert capped == []  # single routine exceeds the tiny budget


def test_match_deterministic_order(tmp_path: Path):
    """Verify matches sort by name so injection is stable across runs."""
    for name, trigger in (("zeta.md", "go"), ("alpha.md", "go")):
        p = tmp_path / name
        content = format_procedure(name.replace(".md", ""), "steps", triggers=[trigger], status=APPROVED)
        p.write_text(content, encoding="utf-8")
    matched = match_procedures(tmp_path, "go")
    assert [p.name for p in matched] == ["alpha", "zeta"]


def test_injection_block_format():
    """Verify the injectable block carries steps plus rationale."""
    from study_agent.study.procedures import Procedure

    assert format_procedures_block([]) == ""
    block = format_procedures_block(
        [Procedure(name="r", steps="1. x", rationale="Because.", version=2)]
    )
    assert "Routine 'r' (v2)" in block
    assert "1. x" in block and "Because." in block


def test_note_reset_prevents_stale_leak():
    """Verify a previous turn's procedure block never leaks into later turns."""
    ctx = ContextManager(custom_system_prompt="System")
    ctx.inject_system_note("facts\n\nApplicable routines:\nRoutine 'x'")
    assert "Routine" in ctx.get_messages()[0].content
    ctx.reset_injected_note()
    assert "Routine" not in ctx.get_messages()[0].content
    assert "System" in ctx.get_messages()[0].content
    assert len(ctx.get_messages()) == 1
