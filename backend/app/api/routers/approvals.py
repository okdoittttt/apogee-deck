"""관리자 메일의 승인·거절 링크 화면.

링크를 열면(GET) 신청 내용과 확인 버튼만 보여주고, 버튼(POST)을 눌러야 처리한다. 메일 보안 검사기나
미리보기가 링크를 자동으로 열어도 승인되지 않게 하기 위해서다. 화면은 백엔드가 직접 그리는 작은 HTML 이다.

링크 주소에 토큰이 들어 있으므로 다른 사이트로 주소가 새지 않게 ``Referrer-Policy: same-origin`` 을
붙이고, 접근 로그에서는 토큰을 가린다 (``app.main``). ``no-referrer`` 를 쓰면 브라우저가 확인 버튼의
POST 에 ``Origin: null`` 을 붙여서 출처 확인(CSRF 방어)에 막힌다.
"""

from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.deps import SessionDep
from app.auth.approval import ApprovalError, decide_by_token, resolve_token
from app.core.config import get_settings

router = APIRouter(prefix="/api/auth/approvals")

ACTION_LABELS = {"approve": "승인", "reject": "거절"}

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # 인라인 스타일만 허용하고 스크립트는 막는다. 폼은 같은 주소로만 보낼 수 있다.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
}


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#faf9f6; color:#1c1b19; margin:0; padding:40px 16px; }}
  main {{ max-width:520px; margin:0 auto; background:#fff; border:1px solid #e3e1db; border-radius:16px; padding:28px; }}
  h1 {{ font-size:20px; margin:0 0 16px; }}
  dl {{ margin:0 0 20px; }} dt {{ color:#6f6c65; font-size:13px; margin-top:12px; }}
  dd {{ margin:4px 0 0; white-space:pre-wrap; word-break:break-word; }}
  button {{ font-size:15px; padding:10px 18px; border-radius:10px; border:0; cursor:pointer; color:#fff; }}
  .approve {{ background:#1f7a4d; }} .reject {{ background:#b42318; }}
  p.note {{ color:#6f6c65; font-size:13px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#262624; color:#ecebe7; }} main {{ background:#31312e; border-color:#393935; }}
    dt, p.note {{ color:#a8a59e; }}
  }}
</style></head>
<body><main>{body}</main></body></html>"""
    return HTMLResponse(html, status_code=status_code, headers=_SECURITY_HEADERS)


@router.get("/{token}", response_class=HTMLResponse)
async def confirm_page(token: str, session: SessionDep) -> HTMLResponse:
    """신청 내용과 승인(또는 거절) 확인 버튼을 보여준다. 이 요청만으로는 아무것도 바뀌지 않는다.

    Args:
        token: 링크의 토큰.
        session: DB 세션.

    Returns:
        확인 화면. 링크가 유효하지 않으면 안내 화면(410).
    """
    try:
        pending = await resolve_token(session, token)
    except ApprovalError as exc:
        return _page(
            "링크를 쓸 수 없습니다", f"<h1>링크를 쓸 수 없습니다</h1><p>{escape(str(exc))}</p>", 410
        )
    label = ACTION_LABELS[pending.action]
    user = pending.user
    body = f"""<h1>가입 신청 {label}</h1>
<dl>
  <dt>이름</dt><dd>{escape(user.name)}</dd>
  <dt>이메일</dt><dd>{escape(user.email)}</dd>
  <dt>신청 사유</dt><dd>{escape(user.request_reason or "(없음)")}</dd>
</dl>
<form method="post">
  <button type="submit" class="{pending.action}">{label}하기</button>
</form>
<p class="note">버튼을 누르면 바로 처리되고 신청자에게 메일이 갑니다. 이 링크는 한 번만 쓸 수 있습니다.</p>"""
    return _page(f"가입 신청 {label}", body)


@router.post("/{token}", response_class=HTMLResponse)
async def decide_page(token: str, session: SessionDep) -> HTMLResponse:
    """링크의 결정(승인·거절)을 처리하고 결과 화면을 보여준다.

    Args:
        token: 링크의 토큰.
        session: DB 세션.

    Returns:
        결과 화면. 링크가 유효하지 않으면 안내 화면(410).
    """
    try:
        user = await decide_by_token(session, token, get_settings())
    except ApprovalError as exc:
        return _page(
            "처리하지 못했습니다",
            f"<h1>처리하지 못했습니다</h1><p>{escape(str(exc))}</p>",
            exc.status_code,
        )
    done = "승인했습니다" if user.status == "approved" else "거절했습니다"
    body = f"""<h1>{escape(user.name)}님의 가입을 {done}</h1>
<p>{escape(user.email)} 에게 결과 메일을 보냈습니다. 이 창은 닫아도 됩니다.</p>"""
    return _page(done, body)
