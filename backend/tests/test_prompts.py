from pathlib import Path

import pytest

from app import prompts
from app.prompts import loader


def test_all_prompts_valid():
    assert "chat/system" in prompts.validate_all()


def test_render_chat_system_includes_shared_fragments():
    text = prompts.render("chat/system", today="2026-01-01 (Thursday)").text
    assert "2026-01-01 (Thursday)" in text
    assert "same language the user writes in" in text  # shared/language
    assert "rendered as Markdown" in text  # shared/formatting
    assert "---" not in text  # frontmatter 가 본문에 섞이지 않음


def test_render_rejects_missing_and_undeclared_variables():
    with pytest.raises(prompts.PromptError, match="누락"):
        prompts.render("chat/system")
    with pytest.raises(prompts.PromptError, match="미선언"):
        prompts.render("chat/system", today="x", extra="y")


def test_unknown_prompt_name():
    with pytest.raises(prompts.PromptError):
        prompts.render("nope/system")
    with pytest.raises(prompts.PromptError):
        prompts.render("chat/does-not-exist")


@pytest.fixture
def isolated_prompt_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """임시 디렉터리에 에이전트/공용 프롬프트를 만들어 로더가 그것만 보게 한다."""
    shared = tmp_path / "shared"
    agents = tmp_path / "agents"
    shared.mkdir()
    (agents / "demo" / "prompts").mkdir(parents=True)
    monkeypatch.setattr(loader, "SHARED_DIR", shared)
    monkeypatch.setattr(loader, "AGENTS_DIR", agents)
    loader._env.cache_clear()
    loader.load_template.cache_clear()
    yield shared, agents / "demo" / "prompts"
    loader._env.cache_clear()
    loader.load_template.cache_clear()


def test_validate_detects_undeclared_variable(isolated_prompt_dirs):
    _, demo = isolated_prompt_dirs
    (demo / "system.md").write_text("---\nvariables: []\n---\nHello {{ name }}\n")
    with pytest.raises(prompts.PromptError, match="variables"):
        prompts.validate_all()


def test_validate_follows_includes(isolated_prompt_dirs):
    shared, demo = isolated_prompt_dirs
    (shared / "greet.md").write_text("---\ndescription: x\n---\nHi {{ name }}\n")
    (demo / "system.md").write_text(
        '---\nvariables: [name]\n---\n{% include "shared/greet.md" %}\n'
    )
    assert prompts.validate_all() == ["demo/system"]
    assert prompts.render("demo/system", name="A").text == "Hi A"


def test_validate_detects_missing_include(isolated_prompt_dirs):
    _, demo = isolated_prompt_dirs
    (demo / "system.md").write_text('{% include "shared/missing.md" %}\n')
    with pytest.raises(prompts.PromptError, match="missing"):
        prompts.validate_all()
