"""Langfuse 프롬프트 연동 검증 (실제 Langfuse 없이 가짜 클라이언트 사용)."""

import re
from dataclasses import dataclass, field

import pytest

from app import prompts
from app.config import Settings
from app.prompts import loader, remote


@dataclass
class FakePrompt:
    name: str
    prompt: str
    version: int
    labels: list[str]
    config: dict = field(default_factory=dict)

    @property
    def variables(self) -> list[str]:
        return re.findall(r"\{\{(\w+)\}\}", self.prompt)

    def compile(self, **kwargs) -> str:
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(kwargs[m.group(1)]), self.prompt)


class FakeLangfuse:
    def __init__(self):
        self.versions: dict[str, list[FakePrompt]] = {}
        self.get_calls = 0
        self.fail = False

    def get_prompt(self, name, *, label=None, **_):
        self.get_calls += 1
        if self.fail:
            raise ConnectionError("langfuse down")
        for p in reversed(self.versions.get(name, [])):
            if label == "latest" or label in p.labels:
                return p
        raise LookupError("404 prompt not found")

    def create_prompt(self, *, name, prompt, labels, config, **_):
        versions = self.versions.setdefault(name, [])
        for p in versions:  # 라벨은 한 버전에만 붙는다
            p.labels = [lb for lb in p.labels if lb not in labels]
        created = FakePrompt(name, prompt, len(versions) + 1, list(labels), config)
        versions.append(created)
        return created

    def move_label(self, name, version, label):
        for p in self.versions[name]:
            p.labels = [lb for lb in p.labels if lb != label]
            if p.version == version:
                p.labels.append(label)

    def clear_prompt_cache(self):
        pass


@pytest.fixture
def fake_langfuse(monkeypatch):
    fake = FakeLangfuse()
    settings = Settings(
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        prompt_source="langfuse",
        prompt_label="production",
    )
    monkeypatch.setattr(remote, "_client", lambda: fake)
    monkeypatch.setattr(remote, "get_settings", lambda: settings)
    monkeypatch.setattr(prompts, "get_settings", lambda: settings)
    monkeypatch.setattr(remote, "_git_commit", lambda: "abc1234")
    remote.reset_failure_state()
    yield fake
    remote.reset_failure_state()


def test_compile_for_langfuse_merges_includes_and_keeps_placeholders():
    text = prompts.compile_for_langfuse("chat/system")
    assert "{{today}}" in text
    assert "same language the user writes in" in text  # shared 조각이 합쳐짐
    assert "{%" not in text


def test_sync_creates_once_then_unchanged(fake_langfuse):
    first = remote.sync_all()
    assert [(r.name, r.status, r.version) for r in first] == [("chat/system", "created", 1)]
    created = fake_langfuse.versions["chat/system"][0]
    assert created.labels == ["production"]
    assert created.config["source_hash"] == remote.content_hash(created.prompt)
    assert created.config["git_commit"] == "abc1234"

    second = remote.sync_all()
    assert second[0].status == "unchanged"
    assert len(fake_langfuse.versions["chat/system"]) == 1


def test_sync_creates_new_version_when_content_changes(fake_langfuse, monkeypatch):
    remote.sync_all()
    monkeypatch.setattr(remote, "compile_for_langfuse", lambda name: "Changed. {{today}}")
    result = remote.sync_all()[0]
    assert (result.status, result.version) == ("created", 2)
    v1, v2 = fake_langfuse.versions["chat/system"]
    assert v1.labels == [] and v2.labels == ["production"]


def test_sync_does_not_undo_a_rollback(fake_langfuse, monkeypatch):
    remote.sync_all()
    monkeypatch.setattr(remote, "compile_for_langfuse", lambda name: "Changed. {{today}}")
    remote.sync_all()
    fake_langfuse.move_label("chat/system", 1, "production")  # Langfuse 화면에서 v1 로 롤백

    result = remote.sync_all()[0]  # 최신(v2)과 내용이 같으므로 아무것도 하지 않는다
    assert result.status == "unchanged"
    assert fake_langfuse.versions["chat/system"][0].labels == ["production"]


def test_render_uses_langfuse_version_and_links_prompt(fake_langfuse):
    remote.sync_all()
    p = prompts.render("chat/system", today="2026-01-01 (Thursday)")
    assert p.source == "langfuse" and p.version == 1
    assert p.langfuse_prompt is fake_langfuse.versions["chat/system"][0]
    assert "2026-01-01 (Thursday)" in p.text
    assert p.text == prompts.render_file("chat/system", today="2026-01-01 (Thursday)").text


def test_render_falls_back_to_file_and_backs_off(fake_langfuse):
    fake_langfuse.fail = True
    p = prompts.render("chat/system", today="x")
    assert p.source == "file" and p.langfuse_prompt is None
    calls = fake_langfuse.get_calls
    prompts.render("chat/system", today="x")
    assert fake_langfuse.get_calls == calls  # 실패 직후엔 잠시 Langfuse 를 다시 부르지 않는다


def test_render_falls_back_when_remote_variables_differ(fake_langfuse):
    fake_langfuse.create_prompt(
        name="chat/system", prompt="Edited in UI {{nickname}}", labels=["production"], config={}
    )
    p = prompts.render("chat/system", today="x")
    assert p.source == "file"


def test_render_falls_back_when_prompt_missing_in_langfuse(fake_langfuse):
    p = prompts.render("chat/system", today="x")  # 아직 sync 전
    assert p.source == "file"


def test_validate_rejects_logic_that_langfuse_cannot_express(tmp_path, monkeypatch):
    shared, agents = tmp_path / "shared", tmp_path / "agents"
    shared.mkdir()
    (agents / "demo" / "prompts").mkdir(parents=True)
    (agents / "demo" / "prompts" / "system.md").write_text(
        "---\nvariables: [vip]\n---\n{% if vip %}VIP{% endif %}\n"
    )
    monkeypatch.setattr(loader, "SHARED_DIR", shared)
    monkeypatch.setattr(loader, "AGENTS_DIR", agents)
    loader._env.cache_clear()
    loader.load_template.cache_clear()
    try:
        with pytest.raises(prompts.PromptError, match="If"):
            prompts.validate_all()
    finally:
        loader._env.cache_clear()
        loader.load_template.cache_clear()
