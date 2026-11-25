from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ROOT_DIR = Path(__file__).resolve().parents[1]
SELF_PATH = PurePosixPath(Path(__file__).resolve().relative_to(ROOT_DIR).as_posix())
SAFE_PLACEHOLDERS = {
    "",
    "change_me",
    "changeme",
    "discord_bot_token",
    "example",
    "pass",
    "password",
    "пароль",
    "replace_me",
    "your_password",
    "your_token",
}

# Credentials that only ever exist inside an ephemeral CI service container.
# Scoped to an exact (path, value) pair on purpose: adding "guinness-test-only"
# to SAFE_PLACEHOLDERS would silence that string everywhere, including a real
# .env, and loosening the scanner is a worse trade than listing the one file
# that legitimately contains it.
CI_CREDENTIAL_ALLOWLIST = {
    (".github/workflows/tests.yml", "guinness-test-only"),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


CONTENT_PATTERNS = (
    (
        "private key",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
    (
        "Discord token",
        re.compile(
            r"(?<![A-Za-z0-9_-])"
            r"(?:mfa\.[A-Za-z0-9_-]{40,}|"
            r"[A-Za-z0-9_-]{20,30}\.[A-Za-z0-9_-]{5,8}\."
            r"[A-Za-z0-9_-]{25,})"
        ),
    ),
)
# MAJESTIC_API_KEY is env-only (header x-api-key). The assignment regex already
# matches *API_KEY with a non-empty value; keep .env.example empty so the
# placeholder does not trip the scanner. TOKEN and DATABASE_URL stay here too.
ENV_ONLY_SECRET_KEYS = (
    "TOKEN",
    "TOKEN_TEST",
    "TOKEN_MAIN",
    "DATABASE_URL",
    "MAJESTIC_API_KEY",
)
_TOML_ENV_SECRET_RE = re.compile(
    r"(?m)^\s*(?:" + "|".join(ENV_ONLY_SECRET_KEYS) + r")\s*="
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?m)^\s*(?:export\s+)?"
    r"[A-Z_][A-Z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|PRIVATE_KEY|API_KEY)"
    r"\s*=\s*[\"']?(?P<value>[^\"'\s#]+)"
)
DATABASE_URL_RE = re.compile(
    r"postgres(?:ql)?(?:\+psycopg)?://"
    r"[^:\s/@]+:(?P<password>[^@\s/]+)@",
    re.IGNORECASE,
)


def _git(*args: str, text: bool = False) -> str | bytes:
    return subprocess.check_output(
        ["git", *args],
        cwd=ROOT_DIR,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
    )


def _paths(mode: str) -> list[str]:
    if mode == "staged":
        output = _git(
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        )
    elif mode == "working":
        output = _git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    else:
        output = _git("ls-files", "-z")
    assert isinstance(output, bytes)
    return [
        item.decode("utf-8", errors="replace")
        for item in output.split(b"\0")
        if item
    ]


def _content(path: str, mode: str) -> bytes:
    if mode == "staged":
        return _git("show", f":{path}")  # type: ignore[return-value]
    return (ROOT_DIR / path).read_bytes()


def _forbidden_path(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    if name == ".env.example" or name.endswith(".env.example"):
        return False
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return True
    return name.endswith((".pem", ".p12", ".pfx")) or (
        name.endswith(".key") and name != "public.key"
    )


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().strip("\"'<>").casefold()
    return (
        normalized in SAFE_PLACEHOLDERS
        or "example" in normalized
        or normalized.startswith("${")
    )


def _is_allowed_ci_credential(path: str, value: str) -> bool:
    return (path, value.strip().strip("\"'")) in CI_CREDENTIAL_ALLOWLIST


def scan(mode: str) -> list[Finding]:
    findings: list[Finding] = []
    for path in _paths(mode):
        normalized_path = PurePosixPath(path)
        if _forbidden_path(path):
            findings.append(Finding(path, 1, "forbidden secret filename"))
            continue
        if normalized_path == SELF_PATH:
            continue
        try:
            raw = _content(path, mode)
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        if path.endswith(".toml"):
            for match in _TOML_ENV_SECRET_RE.finditer(text):
                findings.append(
                    Finding(
                        path,
                        _line_number(text, match.start()),
                        "env-only secret in toml",
                    )
                )
        for kind, pattern in CONTENT_PATTERNS:
            for match in pattern.finditer(text):
                findings.append(
                    Finding(path, _line_number(text, match.start()), kind)
                )
        for match in SECRET_ASSIGNMENT_RE.finditer(text):
            value = match.group("value")
            if not _is_placeholder(value) and not _is_allowed_ci_credential(path, value):
                findings.append(
                    Finding(
                        path,
                        _line_number(text, match.start()),
                        "non-placeholder secret assignment",
                    )
                )
        for match in DATABASE_URL_RE.finditer(text):
            password = match.group("password")
            if not _is_placeholder(password) and not _is_allowed_ci_credential(
                path, password
            ):
                findings.append(
                    Finding(
                        path,
                        _line_number(text, match.start()),
                        "database URL with password",
                    )
                )
    return sorted(set(findings), key=lambda item: (item.path, item.line, item.kind))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fail without printing secret values when Git content looks unsafe"
    )
    parser.add_argument(
        "--mode",
        choices=("tracked", "staged", "working"),
        default="tracked",
        help="scan tracked files, the staged index, or the full working tree",
    )
    args = parser.parse_args()
    findings = scan(args.mode)
    if findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.kind}")
        raise SystemExit(
            "Secret scan failed. Values are intentionally hidden; rotate any "
            "credential that was committed."
        )
    print(f"Secret scan passed ({args.mode} files).")


if __name__ == "__main__":
    main()
