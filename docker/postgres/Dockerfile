# 앱 Postgres + pgvector (프로젝트 문서 검색용 벡터 저장).
#
# 공식 pgvector 이미지(pgvector/pgvector)는 Debian(glibc) 기반이라, alpine(musl)으로 만든 기존 데이터
# 디렉터리를 그대로 붙이면 문자열 정렬 규칙이 달라 텍스트 인덱스가 어긋날 수 있다. 그래서 지금 쓰는
# postgres:17-alpine 에 pgvector 를 소스에서 빌드해 넣는다 (데이터 옮기기 없음).
FROM postgres:17-alpine

# 2주 이상 지난 릴리스로 묶는다 (v0.8.7: 2026-10-01).
ARG PGVECTOR_VERSION=0.8.7

# with_llvm=no: JIT 용 비트코드를 만들지 않는다 (clang/llvm 없이 빌드).
# OPTFLAGS="": -march=native 를 빼서 다른 CPU 에서도 돈다.
RUN apk add --no-cache --virtual .build-deps build-base git \
    && git clone --depth 1 --branch "v${PGVECTOR_VERSION}" https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no \
    && make -C /tmp/pgvector install with_llvm=no \
    && rm -rf /tmp/pgvector \
    && apk del .build-deps
