"""파싱 프로세스 테스트용 대상 함수. spawn 으로 띄운 프로세스가 불러올 수 있게 모듈 최상위에 둔다."""

import os
import time


def sleep_forever(conn, *args):
    time.sleep(60)


def crash(conn, *args):
    os._exit(3)


def echo_size(conn, data, kind, filename, max_pages):
    conn.send(("ok", f"{kind}:{filename}:{len(data)}", max_pages))
    conn.close()
