"""파일 기반 프롬프트 로더.

프롬프트는 이름으로 부른다. 이름의 첫 부분이 네임스페이스다.

- ``chat/system`` → ``app/agents/chat/prompts/system.md``
- ``researcher/planner`` → ``app/agents/researcher/prompts/planner.md``
- ``shared/language`` → ``app/prompts/shared/language.md`` (여러 에이전트 공용 조각)

각 파일은 YAML frontmatter + Jinja2 본문으로 구성한다::

    ---
    description: 기본 채팅 에이전트
    variables: [today]          # 렌더링에 필요한 변수 (검증에 사용)
    ---
    오늘 날짜: {{ today }}
    {% include "shared/language.md" %}

에이전트 코드는 경로가 아니라 이름만 알기 때문에, 저장소를 Langfuse 등으로 바꿔도
에이전트 코드는 고치지 않아도 된다.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jinja2 import (
    BaseLoader,
    Environment,
    FileSystemLoader,
    PrefixLoader,
    StrictUndefined,
    TemplateNotFound,
    meta,
    nodes,
)

APP_DIR = Path(__file__).resolve().parent.parent
SHARED_DIR = APP_DIR / "prompts" / "shared"
AGENTS_DIR = APP_DIR / "agents"
SUFFIX = ".md"


class PromptError(Exception):
    """프롬프트 이름, 파일, 변수, 문법이 잘못됐을 때 발생한다."""


@dataclass(frozen=True)
class PromptTemplate:
    """프롬프트 파일의 frontmatter 정보.

    Attributes:
        name: 프롬프트 이름 (예: ``"chat/system"``).
        description: 프롬프트 설명.
        variables: 렌더링에 필요한 변수 이름.
    """

    name: str
    description: str
    variables: frozenset[str]


@dataclass(frozen=True)
class Prompt:
    """렌더링된 프롬프트.

    Attributes:
        name: 프롬프트 이름.
        text: 렌더링된 텍스트.
        source: 출처. ``"file"`` 또는 ``"langfuse"``.
        version: Langfuse 버전 (``source="langfuse"`` 일 때).
        langfuse_prompt: 트레이스와 프롬프트 버전을 연결하는 데 쓰는 Langfuse PromptClient.
    """

    name: str
    text: str
    source: str = "file"
    version: int | None = None
    langfuse_prompt: Any = field(default=None, compare=False, repr=False)


def _split_frontmatter(source: str) -> tuple[dict[str, Any], str]:
    """파일 내용을 frontmatter 딕셔너리와 본문으로 나눈다."""
    if not source.startswith("---\n"):
        return {}, source
    end = source.find("\n---\n", 4)
    if end == -1:
        raise PromptError("frontmatter 가 닫히지 않았습니다 ('---' 누락)")
    header = yaml.safe_load(source[4:end]) or {}
    if not isinstance(header, dict):
        raise PromptError("frontmatter 는 YAML 매핑이어야 합니다")
    return header, source[end + len("\n---\n") :]


class _FrontmatterStrippingLoader(FileSystemLoader):
    """include 된 조각에서도 frontmatter 가 본문에 섞이지 않도록 제거하고 읽는 로더."""

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, str | None, Callable[[], bool] | None]:
        """Frontmatter 를 뺀 템플릿 본문을 읽는다.

        Args:
            environment: Jinja2 환경.
            template: 네임스페이스 안의 템플릿 경로.

        Returns:
            ``(본문, 파일 경로, 변경 확인 함수)``.
        """
        source, filename, uptodate = super().get_source(environment, template)
        _, body = _split_frontmatter(source)
        return body, filename, uptodate


def _namespaces() -> dict[str, Path]:
    """네임스페이스 이름 → 프롬프트 디렉터리. ``shared`` 와 ``prompts/`` 가 있는 에이전트들."""
    spaces = {"shared": SHARED_DIR}
    for agent_dir in sorted(AGENTS_DIR.iterdir()):
        prompts_dir = agent_dir / "prompts"
        if prompts_dir.is_dir():
            if agent_dir.name in spaces:
                raise PromptError(f"예약된 네임스페이스와 이름이 겹칩니다: {agent_dir.name}")
            spaces[agent_dir.name] = prompts_dir
    return spaces


@cache
def _env() -> Environment:
    """네임스페이스별 디렉터리를 읽는 Jinja2 환경 (프로세스당 하나)."""
    loader: BaseLoader = PrefixLoader(
        {ns: _FrontmatterStrippingLoader(str(path)) for ns, path in _namespaces().items()}
    )
    return Environment(
        loader=loader,
        undefined=StrictUndefined,  # 빠진 변수는 빈 문자열이 아니라 오류
        keep_trailing_newline=True,  # include 한 조각 사이의 빈 줄 유지
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # HTML 이 아니라 LLM 텍스트
    )


def _path_of(name: str) -> Path:
    """프롬프트 이름을 파일 경로로 바꾼다."""
    ns, _, rest = name.partition("/")
    spaces = _namespaces()
    if ns not in spaces or not rest:
        raise PromptError(f"알 수 없는 프롬프트 이름: {name!r} (형식: '<에이전트>/<이름>')")
    return spaces[ns] / f"{rest}{SUFFIX}"


@cache
def load_template(name: str) -> PromptTemplate:
    """프롬프트 파일의 frontmatter 를 읽는다.

    Args:
        name: 프롬프트 이름 (예: ``"chat/system"``).

    Returns:
        frontmatter 정보.

    Raises:
        PromptError: 이름이 잘못됐거나, 파일이 없거나, frontmatter 형식이 잘못됐을 때.
    """
    path = _path_of(name)
    if not path.is_file():
        raise PromptError(f"프롬프트 파일이 없습니다: {name} ({path})")
    header, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
    declared = header.get("variables") or []
    if not isinstance(declared, list):
        raise PromptError(f"{name}: variables 는 리스트여야 합니다")
    return PromptTemplate(
        name=name,
        description=str(header.get("description", "")),
        variables=frozenset(declared),
    )


def render(name: str, /, **variables: Any) -> Prompt:
    """로컬 파일로 프롬프트를 렌더링한다.

    Args:
        name: 프롬프트 이름. positional-only 라서 프롬프트 변수 이름으로 ``name`` 을 써도
            충돌하지 않는다.
        **variables: 템플릿 변수. frontmatter 의 ``variables`` 와 정확히 일치해야 한다.

    Returns:
        ``source="file"`` 인 렌더링된 프롬프트.

    Raises:
        PromptError: 변수가 빠지거나 남을 때, include 대상이 없을 때.
    """
    tpl = load_template(name)
    missing = tpl.variables - variables.keys()
    extra = variables.keys() - tpl.variables
    if missing or extra:
        raise PromptError(
            f"{name}: 변수 불일치 (누락: {sorted(missing) or '-'}, 미선언: {sorted(extra) or '-'})"
        )
    try:
        text = _env().get_template(f"{name}{SUFFIX}").render(**variables)
    except TemplateNotFound as e:
        raise PromptError(f"{name}: include 대상을 찾을 수 없습니다: {e.name}") from e
    return Prompt(name=name, text=text.strip())


# Langfuse 로 동기화할 수 있도록 허용하는 Jinja 문법: 텍스트, 단순 변수 치환, 정적 include.
# 변수 값에 따라 내용이 달라지는 if/for/filter 등은 Langfuse 템플릿({{var}})로 옮길 수 없다.
_ALLOWED_NODES = (
    nodes.Template,
    nodes.Output,
    nodes.TemplateData,
    nodes.Name,
    nodes.Include,
    nodes.Const,
)


def _used_variables(name: str) -> set[str]:
    """Include 를 따라가며 실제로 참조하는 변수를 모으고, 허용된 문법만 썼는지 검사한다."""
    env = _env()
    seen: set[str] = set()
    used: set[str] = set()

    def visit(template_name: str) -> None:
        if template_name in seen:
            return
        seen.add(template_name)
        source, _, _ = env.loader.get_source(env, template_name)  # type: ignore[union-attr]
        ast = env.parse(source)
        for node in ast.find_all(nodes.Node):
            if not isinstance(node, _ALLOWED_NODES):
                raise PromptError(
                    f"{template_name}: '{type(node).__name__}' 문법은 쓸 수 없습니다 "
                    "(허용: 변수 치환 {{ var }}, 정적 include)"
                )
        used.update(meta.find_undeclared_variables(ast))
        for ref in meta.find_referenced_templates(ast):
            if ref is None:
                raise PromptError(f"{template_name}: 동적 include 는 지원하지 않습니다")
            visit(ref)

    visit(f"{name}{SUFFIX}")
    return used


def list_prompts() -> list[str]:
    """에이전트별 프롬프트 이름 목록.

    ``shared`` 조각은 단독으로 쓰지 않으므로 제외한다.

    Returns:
        정렬된 프롬프트 이름 (예: ``["chat/system"]``).
    """
    names = []
    for ns, path in _namespaces().items():
        if ns == "shared":
            continue
        names += [f"{ns}/{p.relative_to(path).with_suffix('')}" for p in path.rglob(f"*{SUFFIX}")]
    return sorted(names)


def compile_for_langfuse(name: str) -> str:
    """Langfuse 에 올릴 최종 템플릿을 만든다.

    include 를 모두 합치고, 변수는 Langfuse 형식(``{{var}}``)으로 남긴다.

    Args:
        name: 프롬프트 이름.

    Returns:
        Langfuse text 프롬프트로 올릴 문자열.
    """
    tpl = load_template(name)
    placeholders = {v: "{{" + v + "}}" for v in tpl.variables}
    text = _env().get_template(f"{name}{SUFFIX}").render(**placeholders)
    return text.strip()


def validate_all() -> list[str]:
    """모든 프롬프트를 정적으로 검사한다.

    frontmatter 형식, include 대상 존재, 선언한 변수와 실제 사용 변수의 일치,
    Langfuse 로 옮길 수 있는 문법만 썼는지를 확인한다. 앱 시작 시와 테스트에서 호출해
    잘못된 프롬프트가 배포되기 전에 잡는다.

    Returns:
        검사한 프롬프트 이름 목록.

    Raises:
        PromptError: 하나라도 실패하면, 실패한 항목을 모두 담아서.
    """
    names = list_prompts()
    errors = []
    for name in names:
        try:
            tpl = load_template(name)
            used = _used_variables(name)
        except (PromptError, TemplateNotFound) as e:
            errors.append(f"{name}: {e}")
            continue
        if used != tpl.variables:
            errors.append(
                f"{name}: 선언된 variables {sorted(tpl.variables)} 와 "
                f"실제 사용 {sorted(used)} 가 다릅니다"
            )
    if errors:
        raise PromptError("프롬프트 검증 실패:\n  " + "\n  ".join(errors))
    return names
