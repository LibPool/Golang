#!/usr/bin/env python3
"""Generate the LibPool Go module index from the Go module index and proxy.

Layout:
  go-v1/<module-path>/<short-name>.md

Run from the repo root:
    python tools/generate_index.py --crawl --crawl-count 30000
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path


INDEX_GOLANG = "https://index.golang.org/index"
PROXY_GOLANG = "https://proxy.golang.org"
PKG_GO_DEV = "https://pkg.go.dev"
USER_AGENT = "LibPool-Indexer/1.0 (+https://github.com/LibPool)"
CACHE_PATH = Path(__file__).resolve().parent / "cache" / "go.json"
INDEX_PATHS = Path(__file__).resolve().parent / "cache" / "go_index_paths.txt"
INDEX_CURSOR = Path(__file__).resolve().parent / "cache" / "go_index_cursor.json"
GO_RELEASES = ["go-v1"]


@dataclass
class GoLib:
    path: str
    description: str = ""
    homepage: str = ""
    source_url: str = ""
    current_version: str = ""
    versions: list[str] = field(default_factory=list)
    go_version: str = ""

    @property
    def safe_path(self) -> str:
        return self.path


def http_json(url: str) -> dict | None:
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:
            last_exc = exc
            if getattr(exc, "code", None) in (429, 500, 502, 503, 504):
                time.sleep(1 + attempt * 2)
                continue
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
            break
    print(f"  fetch failed: {url} -> {last_exc}", flush=True)
    return None


def fetch_lines(url: str) -> tuple[list[str], str]:
    """Fetch line data. Error kind: "" ok, "notfound" permanent, "network" transient."""
    last_exc: Exception | None = None
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp:
                lines = [ln for ln in resp.read().decode("utf-8", errors="replace").splitlines() if ln.strip()]
                return lines, ""
        except Exception as exc:
            last_exc = exc
            code = getattr(exc, "code", None)
            if code == 404:
                print(f"  fetch failed: {url} -> HTTP Error 404: Not Found", flush=True)
                return [], "notfound"
            if code in (429, 500, 502, 503, 504):
                time.sleep(2 + attempt * 3)
                continue
            if attempt < 6:
                time.sleep(3 + attempt * 3)
                continue
            break
    print(f"  fetch failed: {url} -> {last_exc}", flush=True)
    return [], "network"


def http_lines(url: str) -> list[str]:
    return fetch_lines(url)[0]


def version_key(v: str) -> tuple:
    core = v.lstrip("v")
    nums = re.findall(r"\d+", core)
    ints = [int(x) for x in nums[:3]]
    while len(ints) < 3:
        ints.append(0)
    pre = 0 if not re.search(r"(?:alpha|beta|rc|pre|dev)", core, re.I) else 1
    return (ints[0], ints[1], ints[2], pre, core)


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def unescape_module(escaped: str) -> str:
    """Reverse the Go module proxy uppercase escaping (!x -> X)."""
    out: list[str] = []
    i = 0
    while i < len(escaped):
        ch = escaped[i]
        if ch == "!" and i + 1 < len(escaped):
            out.append(escaped[i + 1].upper())
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def import_failures(log_path: Path, cache: dict) -> int:
    """Cache modules whose proxy version list returned 404 in a previous run."""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    imported = 0
    pattern = re.compile(r"fetch failed: https://proxy\.golang\.org/([^ ]+)/@v/list -> HTTP Error 404")
    for m in pattern.finditer(text):
        path = unescape_module(m.group(1))
        if path and path not in cache:
            cache[path] = {"failed": True, "current_version": "", "versions": []}
            imported += 1
    return imported


def save_index_cursor(since: str) -> None:
    INDEX_CURSOR.parent.mkdir(parents=True, exist_ok=True)
    INDEX_CURSOR.write_text(json.dumps({"since": since}, separators=(",", ":")), encoding="utf-8")


def crawl_index(limit: int, exclude: set[str]) -> tuple[list[str], bool]:
    """Crawl index.golang.org to the end with a persisted cursor and retries."""
    INDEX_PATHS.parent.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    seen: set[str] = set(exclude)
    existing = []
    if INDEX_PATHS.exists():
        with INDEX_PATHS.open(encoding="utf-8", errors="replace") as fh:
            existing = [ln.strip() for ln in fh if ln.strip()]
    for p in existing:
        if p not in seen:
            seen.add(p)
    out = INDEX_PATHS.open("a", encoding="utf-8", newline="\n")
    persisted = set(existing)
    cursor = None
    if INDEX_CURSOR.exists():
        try:
            cursor = json.loads(INDEX_CURSOR.read_text(encoding="utf-8")).get("since")
        except Exception:
            cursor = None
    if cursor:
        url = f"{INDEX_GOLANG}?since={urllib.parse.quote(cursor)}&limit=1000"
    else:
        url = f"{INDEX_GOLANG}?limit=1000"
    pages = 0
    print(f"Crawling Go index, target {limit} unique modules...", flush=True)
    reached_end = False
    consecutive_failures = 0
    while len(paths) + len(persisted) < limit:
        data, err = fetch_lines(url)
        if not data:
            consecutive_failures += 1
            if err == "notfound":
                reached_end = True
                break
            if consecutive_failures >= 5:
                print(f"  giving up after {consecutive_failures} consecutive failures", flush=True)
                break
            print(f"  page {pages + 1} failed ({err}); retrying in 8s", flush=True)
            time.sleep(8)
            continue
        consecutive_failures = 0
        pages += 1
        last_ts = ""
        for line in data:
            try:
                row = json.loads(line)
            except Exception:
                continue
            path = (row.get("Path") or "").strip()
            ts = (row.get("Timestamp") or "").strip()
            if ts:
                last_ts = ts
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
                if path not in persisted:
                    out.write(path + "\n")
                    persisted.add(path)
                if len(paths) + len(persisted) >= limit:
                    break
        print(f"  index page {pages}: {len(paths):,} new so far", flush=True)
        if not last_ts:
            reached_end = True
            break
        if len(data) < 1000:
            reached_end = True
            save_index_cursor(last_ts)
            break
        save_index_cursor(last_ts)
        url = f"{INDEX_GOLANG}?since={urllib.parse.quote(last_ts)}&limit=1000"
        time.sleep(0.2)
    out.close()
    return paths, reached_end


def source_url_for(path: str) -> str:
    for host in ("github.com", "gitlab.com", "bitbucket.org", "gitee.com", "codeberg.org"):
        prefix = host + "/"
        if path.startswith(prefix):
            parts = path[len(prefix) :].split("/")
            if len(parts) >= 2:
                return f"https://{host}/{parts[0]}/{parts[1]}"
    if path.startswith("golang.org/x/"):
        return f"https://go.googlesource.com/{path.split('/')[-1]}"
    return ""


def mod_go_version(path: str, version: str) -> str:
    url = f"{PROXY_GOLANG}/{quote_module(path)}/@v/{version}.mod"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        m = re.search(r"^go\s+(\S+)", text, re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def proxy_path(path: str) -> str:
    """Go module proxy escaping: uppercase letters become !lowercase."""
    out: list[str] = []
    for ch in path:
        if "A" <= ch <= "Z":
            out.append("!" + ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def quote_module(path: str) -> str:
    return urllib.parse.quote(proxy_path(path), safe="/@.!-_.:[]")


def enrich_one(lib: GoLib, cache: dict, use_cache: bool, fetch_mod: bool) -> tuple[GoLib, dict | None]:
    key = lib.path
    if use_cache and key in cache:
        entry = cache[key]
        for attr in ("description", "homepage", "source_url", "current_version", "versions", "go_version"):
            setattr(lib, attr, entry.get(attr, ""))
        if entry.get("failed") or lib.current_version or lib.versions:
            return lib, None

    rows, err = fetch_lines(f"{PROXY_GOLANG}/{quote_module(lib.path)}/@v/list")
    vs = [r for r in rows if r.strip()]
    if not vs:
        if err in ("", "notfound"):
            # 404 or a version-less proxy entry is permanent; cache to skip next run.
            return lib, {"failed": True, "current_version": "", "versions": []}
        return lib, None
    vs_sorted = sorted(set(vs), key=version_key)
    lib.versions = vs_sorted
    lib.current_version = vs_sorted[-1]
    if not lib.source_url:
        lib.source_url = source_url_for(lib.path)
    if not lib.homepage:
        lib.homepage = ""
    if fetch_mod:
        lib.go_version = mod_go_version(lib.path, lib.current_version)
    if not lib.description:
        lib.description = f"Go module，由 index.golang.org 收录：{lib.path}。"
    entry = {
        "version": lib.current_version,
        "description": lib.description,
        "homepage": lib.homepage,
        "source_url": lib.source_url,
        "current_version": lib.current_version,
        "versions": lib.versions[-40:],
        "go_version": lib.go_version,
    }
    return lib, entry


def seed_libs() -> list[GoLib]:
    rows = [
        ("github.com/gin-gonic/gin", "Gin is a HTTP web framework written in Go", "https://gin-gonic.com", "https://github.com/gin-gonic/gin"),
        ("github.com/labstack/echo", "High performance, minimalist Go web framework", "https://echo.labstack.com", "https://github.com/labstack/echo"),
        ("github.com/gofiber/fiber", "Express inspired web framework written in Go", "https://gofiber.io", "https://github.com/gofiber/fiber"),
        ("github.com/gorilla/mux", "A powerful URL router and dispatcher for Go", "https://www.gorillatoolkit.org/pkg/mux", "https://github.com/gorilla/mux"),
        ("github.com/spf13/cobra", "A Commander for modern Go CLI interactions", "https://cobra.dev", "https://github.com/spf13/cobra"),
        ("github.com/spf13/viper", "Go configuration with fangs", "https://github.com/spf13/viper", "https://github.com/spf13/viper"),
        ("github.com/golang-jwt/jwt/v5", "Go implementation of JSON Web Tokens", "https://github.com/golang-jwt/jwt", "https://github.com/golang-jwt/jwt"),
        ("github.com/golang-jwt/jwt", "Go implementation of JSON Web Tokens", "https://github.com/golang-jwt/jwt", "https://github.com/golang-jwt/jwt"),
        ("github.com/zeromicro/go-zero", "A cloud-native Go microservices framework", "https://go-zero.dev", "https://github.com/zeromicro/go-zero"),
        ("github.com/grpc/grpc-go", "The Go language implementation of gRPC", "https://grpc.io", "https://github.com/grpc/grpc-go"),
        ("google.golang.org/grpc", "The Go language implementation of gRPC", "https://grpc.io", "https://github.com/grpc/grpc-go"),
        ("github.com/stretchr/testify", "A toolkit with common assertions and mocks for Go", "https://github.com/stretchr/testify", "https://github.com/stretchr/testify"),
        ("github.com/golang-migrate/migrate", "Database migrations written in Go", "https://github.com/golang-migrate/migrate", "https://github.com/golang-migrate/migrate"),
        ("gorm.io/gorm", "The fantastic ORM library for Golang", "https://gorm.io", "https://github.com/go-gorm/gorm"),
        ("github.com/jmoiron/sqlx", "General purpose extensions to database/sql", "https://github.com/jmoiron/sqlx", "https://github.com/jmoiron/sqlx"),
        ("github.com/jackc/pgx/v5", "PostgreSQL driver and toolkit for Go", "https://github.com/jackc/pgx", "https://github.com/jackc/pgx"),
        ("github.com/redis/go-redis/v9", "Type-safe Redis client for Go", "https://redis.uptrace.dev/", "https://github.com/redis/go-redis"),
        ("github.com/google/uuid", "Go package for UUIDs", "https://github.com/google/uuid", "https://github.com/google/uuid"),
        ("github.com/sirupsen/logrus", "Structured, pluggable logging for Go", "https://github.com/sirupsen/logrus", "https://github.com/sirupsen/logrus"),
        ("go.uber.org/zap", "Blazing fast, structured, leveled logging in Go", "https://github.com/uber-go/zap", "https://github.com/uber-go/zap"),
        ("github.com/rs/zerolog", "Zero Allocation JSON Logger", "https://github.com/rs/zerolog", "https://github.com/rs/zerolog"),
        ("github.com/prometheus/client_golang", "Prometheus instrumentation library for Go", "https://prometheus.io", "https://github.com/prometheus/client_golang"),
        ("github.com/opentracing/opentracing-go", "OpenTracing API for Go", "https://opentracing.io", "https://github.com/opentracing/opentracing-go"),
        ("go.opentelemetry.io/otel", "OpenTelemetry Go API and SDK", "https://opentelemetry.io", "https://github.com/open-telemetry/opentelemetry-go"),
        ("github.com/google/go-cmp", "Powerful tools for comparing Go values", "https://github.com/google/go-cmp", "https://github.com/google/go-cmp"),
        ("golang.org/x/tools", "Go tools and libraries", "https://pkg.go.dev/golang.org/x/tools", "https://go.googlesource.com/tools"),
        ("golang.org/x/sync", "Additional synchronization primitives", "https://pkg.go.dev/golang.org/x/sync", "https://go.googlesource.com/sync"),
        ("golang.org/x/text", "Go text processing libraries", "https://pkg.go.dev/golang.org/x/text", "https://go.googlesource.com/text"),
        ("golang.org/x/net", "Go supplemental networking libraries", "https://pkg.go.dev/golang.org/x/net", "https://go.googlesource.com/net"),
        ("github.com/go-playground/validator/v10", "Go struct and field validation", "https://github.com/go-playground/validator", "https://github.com/go-playground/validator"),
        ("github.com/golang/mock", "Go mocking framework", "https://github.com/golang/mock", "https://github.com/golang/mock"),
        ("github.com/joho/godotenv", "A Go port of the Ruby dotenv library", "https://github.com/joho/godotenv", "https://github.com/joho/godotenv"),
        ("github.com/urfave/cli/v2", "A simple, fast, and fun package for building command line apps", "https://cli.urfave.org", "https://github.com/urfave/cli"),
        ("github.com/go-redis/redis/v8", "Type-safe Redis client for Go", "https://github.com/redis/go-redis", "https://github.com/redis/go-redis"),
        ("github.com/golang/protobuf", "Go support for Google protocol buffers", "https://developers.google.com/protocol-buffers", "https://github.com/golang/protobuf"),
        ("google.golang.org/protobuf", "Go support for Google protocol buffers", "https://developers.google.com/protocol-buffers", "https://github.com/protocolbuffers/protobuf-go"),
        ("github.com/nats-io/nats.go", "Go client for NATS", "https://nats.io", "https://github.com/nats-io/nats.go"),
        ("github.com/gorilla/websocket", "Go implementation of WebSocket", "https://github.com/gorilla/websocket", "https://github.com/gorilla/websocket"),
        ("github.com/hashicorp/terraform-plugin-sdk/v2", "Terraform Plugin SDK", "https://developer.hashicorp.com/terraform/plugin", "https://github.com/hashicorp/terraform-plugin-sdk"),
        ("github.com/go-sql-driver/mysql", "MySQL driver for Go's database/sql", "https://github.com/go-sql-driver/mysql", "https://github.com/go-sql-driver/mysql"),
        ("github.com/lib/pq", "Pure Go Postgres driver for database/sql", "https://github.com/lib/pq", "https://github.com/lib/pq"),
        ("github.com/mattn/go-sqlite3", "SQLite3 driver for go using database/sql", "https://github.com/mattn/go-sqlite3", "https://github.com/mattn/go-sqlite3"),
        ("github.com/docker/docker", "The Docker engine and SDK for Go", "https://docker.io", "https://github.com/moby/moby"),
        ("k8s.io/client-go", "Go client for Kubernetes", "https://kubernetes.io", "https://github.com/kubernetes/client-go"),
        ("k8s.io/api", "Kubernetes API types", "https://kubernetes.io", "https://github.com/kubernetes/api"),
        ("github.com/gomodule/redigo", "Go client for Redis", "https://github.com/gomodule/redigo", "https://github.com/gomodule/redigo"),
        ("github.com/IBM/sarama", "Kafka client library for Go", "https://github.com/IBM/sarama", "https://github.com/IBM/sarama"),
        ("github.com/gin-contrib/cors", "CORS middleware for Gin", "https://github.com/gin-contrib/cors", "https://github.com/gin-contrib/cors"),
        ("github.com/go-chi/chi/v5", "Lightweight, idiomatic and composable router", "https://go-chi.io", "https://github.com/go-chi/chi"),
        ("github.com/gorilla/handlers", "A collection of useful middleware for Go HTTP servers", "https://github.com/gorilla/handlers", "https://github.com/gorilla/handlers"),
        ("github.com/iancoleman/strcase", "String case conversion for Go", "https://github.com/iancoleman/strcase", "https://github.com/iancoleman/strcase"),
        ("github.com/asaskevich/govalidator", "Validators and sanitizers for strings", "https://github.com/asaskevich/govalidator", "https://github.com/asaskevich/govalidator"),
        ("github.com/mitchellh/mapstructure", "Go library for decoding generic map values", "https://github.com/mitchellh/mapstructure", "https://github.com/mitchellh/mapstructure"),
        ("github.com/fsnotify/fsnotify", "Cross-platform file system notifications in Go", "https://github.com/fsnotify/fsnotify", "https://github.com/fsnotify/fsnotify"),
        ("github.com/imroc/req/v3", "A simple and powerful HTTP client for Go", "https://github.com/imroc/req", "https://github.com/imroc/req"),
        ("github.com/go-resty/resty/v2", "Simple HTTP and REST client library for Go", "https://github.com/go-resty/resty", "https://github.com/go-resty/resty"),
        ("github.com/yuin/goldmark", "A markdown parser written in Go", "https://github.com/yuin/goldmark", "https://github.com/yuin/goldmark"),
        ("github.com/gorilla/sessions", "Package gorilla/sessions provides cookie and filesystem sessions", "https://github.com/gorilla/sessions", "https://github.com/gorilla/sessions"),
        ("github.com/ethereum/go-ethereum", "Official Go implementation of the Ethereum protocol", "https://geth.ethereum.org", "https://github.com/ethereum/go-ethereum"),
    ]
    libs = []
    for path, desc, homepage, source in rows:
        libs.append(GoLib(path=path, description=desc, homepage=homepage, source_url=source))
    return libs


def readme_md(lib: GoLib) -> str:
    version_lines = "\n".join(f"- {v}" for v in lib.versions[-20:] or ["-"])
    if len(lib.versions) > 20:
        version_lines += f"\n- 共 {len(lib.versions)} 个版本，完整清单见 Go module proxy。"
    websites = []
    if lib.homepage:
        websites.append(f"- 官网：{lib.homepage}")
    websites.append(f"- Go 文档：{PKG_GO_DEV}/{urllib.parse.quote(lib.path, safe='/')}")
    if lib.source_url:
        websites.append(f"- 源码仓库：{lib.source_url}")

    reqs = [
        f"- go mod 下载：`go get {lib.path}@{lib.current_version}`",
        f"- 模块代理：{PROXY_GOLANG}/{quote_display(lib.path)}/@v/list",
    ]
    if lib.go_version:
        reqs.append(f"- go.mod 记录的最低 Go 版本：{lib.go_version}")
    tags = sorted(set(["go", "golang", lib.path.split("/")[0].split(".")[-1]] + [part for part in lib.path.split("/") if len(part) < 30]))
    desc = lib.description or f"Go module {lib.path}（通过 index.golang.org 与 proxy.golang.org 收录）。"
    compat = "Go 语言目前为大版本 1.x；本库收录于 go-v1。"
    return f"""# {lib.path}

> 标签: {", ".join(tags[:12])}

## 简介

{desc}

{compat}

## 官网

{chr(10).join(websites)}

## 历史版本号

- 当前版本：{lib.current_version or "未知"}

{version_lines}

## 获取地址

{chr(10).join(reqs)}
"""


def quote_display(path: str) -> str:
    return urllib.parse.quote(proxy_path(path), safe="/")


def safe_segment(segment: str) -> str:
    """Escape a module path segment for case-insensitive filesystems.

    Uses the Go module proxy convention (uppercase X -> !x) so modules that
    differ only by letter case can coexist on Windows/macOS.
    """
    out: list[str] = []
    for ch in segment:
        if "A" <= ch <= "Z":
            out.append("!" + ch.lower())
        elif ch in '<>:"|?*' or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)
    return "".join(out).rstrip(" .") or "_"


def generate(root: Path, libs: list[GoLib]) -> dict[str, int]:
    counts = defaultdict(int)
    for lib in libs:
        if not lib.current_version:
            continue
        text = readme_md(lib)
        parts = [safe_segment(p) for p in lib.path.split("/")]
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", parts[-1]) or "module"
        target = root / "go-v1" / Path(*parts)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{name}.md").write_text(text, encoding="utf-8")
        counts["go-v1"] += 1
    return dict(counts)


def count_md_files(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for release in GO_RELEASES:
        base = root / release
        counts[release] = len(list(base.rglob("*.md"))) if base.exists() else 0
    return counts


def write_go_readme(root: Path, libs: list[GoLib], counts: dict[str, int], crawl_count: int) -> None:
    total = sum(counts.values())
    lines = [
        "# Go 库索引",
        "",
        "本目录收录来自 Go 官方模块服务的 Go 库索引，按 Go 大版本与模块路径组织：",
        "",
        "- 大版本目录：`go-v1`（Go 语言目前为 1.x 大版本）",
        "- 模块路径：`github.com/gin-gonic/gin` 位于 `go-v1/github.com/gin-gonic/gin/gin.md`",
        "- 含大写字母的模块路径按 Go module proxy 约定转义（如 `gin-Gonic` 写作 `gin-!gonic`），保证大小写不同的模块可共存",
        "- 收录来源：`index.golang.org` 模块索引 + `proxy.golang.org` 模块代理",
        f"- 当前共收录 {total} 个 Go 模块。",
        "",
        "## 数据源",
        "",
        "- Go module index：https://index.golang.org/index",
        "- Go module proxy：https://proxy.golang.org/",
        "- pkg.go.dev：https://pkg.go.dev/",
        "",
        "## 生成方式",
        "",
        "```bash",
        f"python tools/generate_index.py --crawl --crawl-count {crawl_count}",
        "```",
        "",
        "按大版本统计：",
        "",
    ]
    lines += [f"- {k}：{v} 个模块" for k, v in counts.items()]
    lines += ["", "数据来自 Go 官方模块索引与代理，可通过上述命令重新生成。", ""]
    (root / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--crawl", action="store_true")
    ap.add_argument("--crawl-count", type=int, default=30000)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--refresh-cache", action="store_true")
    ap.add_argument("--no-mod-go-version", action="store_true")
    ap.add_argument("--import-failures", metavar="LOG", help="import 404 failures from a previous crawl log and exit")
    ap.add_argument("--clean", action="store_true", help="remove the go-v1 output tree before generating")
    args = ap.parse_args()

    root = Path(args.out).resolve()
    cache = load_cache()

    if args.import_failures:
        imported = import_failures(Path(args.import_failures), cache)
        save_cache(cache)
        print(f"Imported {imported} permanent failures into cache.", flush=True)
        return 0

    refresh_mod = not args.no_mod_go_version
    libs = seed_libs()
    seed_paths = {lib.path for lib in libs}
    if args.crawl:
        exclude = set(cache.keys()) | seed_paths
        paths, reached_end = crawl_index(args.crawl_count, exclude)
        for p in paths:
            libs.append(GoLib(path=p))
        if not reached_end:
            print(f"Crawl did not reach the end (new modules: {len(paths):,}); README not updated.", flush=True)
    else:
        libs += [GoLib(path=k) for k in cache.keys() if k not in seed_paths]
    use_cache = not args.refresh_cache
    print(f"Processing {len(libs)} modules...", flush=True)
    results: list[tuple[GoLib, dict | None]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(enrich_one, lib, cache, use_cache, fetch_mod=refresh_mod) for lib in libs]
        for i, fut in enumerate(as_completed(futures), 1):
            lib, entry = fut.result()
            results.append((lib, entry))
            if entry and (entry.get("failed") or entry.get("current_version")):
                cache[lib.path] = entry
            if i % 500 == 0 or i == len(futures):
                print(f"  enriched {i}/{len(futures)}", flush=True)
            if i % 5000 == 0:
                save_cache(cache)
    save_cache(cache)
    if args.clean:
        shutil.rmtree(root / "go-v1", ignore_errors=True)
    counts = generate(root, [lib for lib, _ in results])
    print("Generated per Go major:", json.dumps(counts, sort_keys=True), flush=True)
    if not args.crawl or reached_end:
        file_counts = count_md_files(root)
        print("On-disk per Go major:", json.dumps(file_counts, sort_keys=True), flush=True)
        write_go_readme(root, [lib for lib, _ in results], file_counts, args.crawl_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
