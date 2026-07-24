#!/usr/bin/env python3

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib import error, parse, request

import tomllib
from packaging.version import InvalidVersion, Version


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_RAW_ROOT = "https://raw.githubusercontent.com"
PYPI_API_TEMPLATE = "https://pypi.org/pypi/{name}/json"
ORG_URL_PATTERN = re.compile(r"https?://github\.com/HistoricEngland/[^\s)\]>'\"]+", re.IGNORECASE)
PYPI_PROJECT_PATTERN = re.compile(r"https?://pypi\.org/project/([^/]+)/?", re.IGNORECASE)
PYPI_BADGE_PATTERN = re.compile(r"(?:img\.shields\.io|badge\.fury\.io)/(?:pypi|py)/v/([^\s)\]>'\"]+)", re.IGNORECASE)
SETUP_NAME_PATTERN = re.compile(r"name\s*=\s*[\"']([^\"']+)[\"']")
REPO_LINK_PATTERN_TEMPLATE = r'<h3[^>]*>\s*<a[^>]+href="/{org}/([^"/]+)"'
RETRY_DELAYS = (1.0, 2.0, 4.0)
DEFAULT_ORG = "HistoricEngland"


@dataclass(frozen=True)
class Repo:
    name: str
    html_url: str
    default_branch: str
    archived: bool


@dataclass(frozen=True)
class Candidate:
    name: str
    source: str


@dataclass(frozen=True)
class PackageStatus:
    name: str
    project_url: str
    latest_stable: str
    newer_prereleases: list[str]
    source_url: str
    source_label: str


class HttpClient:
    def __init__(self, token: str | None, verbose: bool = False) -> None:
        self._token = token
        self._verbose = verbose

    def fetch_json(self, url: str, extra_headers: dict[str, str] | None = None) -> dict | list:
        response_text = self._fetch_text(url, extra_headers=extra_headers)
        return json.loads(response_text)

    def _fetch_text(self, url: str, extra_headers: dict[str, str] | None = None) -> str:
        headers = {
            "Accept": "application/vnd.github+json, application/json",
            "User-Agent": "he-pypi-pub-stat",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if extra_headers:
            headers.update(extra_headers)

        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0, *RETRY_DELAYS), start=1):
            if delay:
                time.sleep(delay)
            try:
                req = request.Request(url, headers=headers)
                with request.urlopen(req) as response:
                    return response.read().decode("utf-8")
            except error.HTTPError as exc:
                if exc.code == 404:
                    raise
                if exc.code in {401, 403, 429, 500, 502, 503, 504}:
                    last_error = exc
                    if self._verbose:
                        print(f"Retrying {url} after HTTP {exc.code} (attempt {attempt})", file=sys.stderr)
                    continue
                raise
            except error.URLError as exc:
                last_error = exc
                if self._verbose:
                    print(f"Retrying {url} after network error: {exc.reason} (attempt {attempt})", file=sys.stderr)
                continue

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Failed to fetch {url}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Markdown status page for HistoricEngland PyPI packages."
    )
    parser.add_argument("--org", default=DEFAULT_ORG, help="GitHub organisation to inspect")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "status.md"),
        help="Path to the generated Markdown file",
    )
    parser.add_argument("--verbose", action="store_true", help="Print diagnostic information")
    return parser.parse_args()


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def list_repositories(client: HttpClient, org: str) -> list[Repo]:
    try:
        return list_repositories_via_api(client, org)
    except error.HTTPError as exc:
        if exc.code != 403:
            raise
        if client._verbose:
            print(
                f"GitHub API rate limit reached for {org}; falling back to public HTML discovery",
                file=sys.stderr,
            )
        return list_repositories_via_html(client, org)


def list_repositories_via_api(client: HttpClient, org: str) -> list[Repo]:
    repos: list[Repo] = []
    page = 1
    while True:
        url = f"{GITHUB_API_ROOT}/orgs/{parse.quote(org)}/repos?per_page=100&page={page}&type=public"
        payload = client.fetch_json(url)
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            repos.append(
                Repo(
                    name=item["name"],
                    html_url=item["html_url"],
                    default_branch=item.get("default_branch", "main"),
                    archived=bool(item.get("archived", False)),
                )
            )
        page += 1
    return repos


def list_repositories_via_html(client: HttpClient, org: str) -> list[Repo]:
    repos: list[Repo] = []
    seen: set[str] = set()
    page = 1
    repo_link_pattern = re.compile(REPO_LINK_PATTERN_TEMPLATE.format(org=re.escape(org)), re.IGNORECASE)

    while True:
        url = f"https://github.com/orgs/{parse.quote(org)}/repositories?type=all&page={page}"
        page_text = client._fetch_text(url, extra_headers={"Accept": "text/html"})
        page_repo_names = repo_link_pattern.findall(page_text)
        new_repo_names = [name for name in page_repo_names if name not in seen]
        if not new_repo_names:
            break
        for name in new_repo_names:
            seen.add(name)
            repos.append(
                Repo(
                    name=name,
                    html_url=f"https://github.com/{org}/{name}",
                    default_branch="HEAD",
                    archived=False,
                )
            )
        page += 1

    return repos


def fetch_repository_file(client: HttpClient, org: str, repo: Repo, path: str) -> str | None:
    encoded_path = "/".join(parse.quote(part) for part in path.split("/"))
    branch = "HEAD"
    url = f"{GITHUB_RAW_ROOT}/{parse.quote(org)}/{parse.quote(repo.name)}/{branch}/{encoded_path}"
    try:
        return client._fetch_text(url, extra_headers={"Accept": "text/plain"})
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def extract_candidates(repo: Repo, file_contents: dict[str, str]) -> list[Candidate]:
    ordered: list[Candidate] = []
    seen: set[str] = set()

    def add(name: str | None, source: str) -> None:
        if not name:
            return
        candidate = name.strip()
        if not candidate:
            return
        key = normalize_name(candidate)
        if key in seen:
            return
        seen.add(key)
        ordered.append(Candidate(name=candidate, source=source))

    pyproject_text = file_contents.get("pyproject.toml")
    if pyproject_text:
        try:
            data = tomllib.loads(pyproject_text)
        except tomllib.TOMLDecodeError:
            data = {}
        if isinstance(data, dict):
            project = data.get("project")
            if isinstance(project, dict):
                add(project.get("name"), "pyproject.toml")
            tool = data.get("tool")
            if isinstance(tool, dict):
                poetry = tool.get("poetry")
                if isinstance(poetry, dict):
                    add(poetry.get("name"), "pyproject.toml [tool.poetry]")

    setup_cfg_text = file_contents.get("setup.cfg")
    if setup_cfg_text:
        parser = configparser.ConfigParser()
        try:
            parser.read_string(setup_cfg_text)
        except configparser.Error:
            parser = configparser.ConfigParser()
        if parser.has_option("metadata", "name"):
            add(parser.get("metadata", "name"), "setup.cfg")

    setup_py_text = file_contents.get("setup.py")
    if setup_py_text:
        match = SETUP_NAME_PATTERN.search(setup_py_text)
        if match:
            add(match.group(1), "setup.py")

    for readme_name in ("README.md", "README.rst", "readme.md"):
        readme_text = file_contents.get(readme_name)
        if not readme_text:
            continue
        for pattern, label in (
            (PYPI_PROJECT_PATTERN, f"{readme_name} project link"),
            (PYPI_BADGE_PATTERN, f"{readme_name} badge"),
        ):
            for match in pattern.finditer(readme_text):
                add(match.group(1), label)

    add(repo.name, "repository name")
    add(repo.name.replace("-", "_"), "repository name variant")
    add(repo.name.replace("_", "-"), "repository name variant")

    return ordered


def build_name_variants(name: str) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in (name, name.replace("-", "_"), name.replace("_", "-"), normalize_name(name)):
        if candidate not in seen:
            variants.append(candidate)
            seen.add(candidate)
    return variants


def find_historic_england_url(info: dict, repo_name: str) -> tuple[str | None, str | None]:
    urls: list[str] = []
    project_urls = info.get("project_urls")
    if isinstance(project_urls, dict):
        urls.extend(value for value in project_urls.values() if isinstance(value, str))
    home_page = info.get("home_page")
    if isinstance(home_page, str) and home_page:
        urls.append(home_page)

    repo_pattern = re.compile(rf"https?://github\.com/HistoricEngland/{re.escape(repo_name)}(?:[/?#]|$)", re.IGNORECASE)
    for url in urls:
        if repo_pattern.search(url):
            return f"https://github.com/HistoricEngland/{repo_name}", f"HistoricEngland/{repo_name}"
    return None, None


def parse_releases(releases: dict) -> tuple[str, list[str]]:
    stable_versions: list[Version] = []
    prerelease_versions: list[Version] = []

    for version_text, files in releases.items():
        if not files:
            continue
        try:
            version = Version(version_text)
        except InvalidVersion:
            continue
        if version.is_prerelease:
            prerelease_versions.append(version)
        else:
            stable_versions.append(version)

    latest_stable_version = max(stable_versions) if stable_versions else None
    if latest_stable_version is None:
        latest_stable = "-"
        newer_prereleases = sorted(prerelease_versions, reverse=True)
    else:
        latest_stable = str(latest_stable_version)
        newer_prereleases = sorted(
            (version for version in prerelease_versions if version > latest_stable_version),
            reverse=True,
        )

    return latest_stable, [str(version) for version in newer_prereleases]


def resolve_package_status(
    client: HttpClient,
    repo: Repo,
    candidates: Iterable[Candidate],
    verbose: bool,
) -> PackageStatus | None:
    for candidate in candidates:
        for variant in build_name_variants(candidate.name):
            try:
                payload = client.fetch_json(PYPI_API_TEMPLATE.format(name=parse.quote(variant)))
            except error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise

            if not isinstance(payload, dict):
                continue

            info = payload.get("info")
            releases = payload.get("releases")
            if not isinstance(info, dict) or not isinstance(releases, dict):
                continue

            source_url, source_label = find_historic_england_url(info, repo.name)
            if not source_url or not source_label:
                if verbose:
                    print(
                        f"Skipping PyPI project {variant!r} from {repo.name}: no HistoricEngland source URL in metadata",
                        file=sys.stderr,
                    )
                continue

            latest_stable, newer_prereleases = parse_releases(releases)
            package_name = info.get("name") if isinstance(info.get("name"), str) else variant
            return PackageStatus(
                name=package_name,
                project_url=f"https://pypi.org/project/{parse.quote(package_name)}/",
                latest_stable=latest_stable,
                newer_prereleases=newer_prereleases,
                source_url=source_url,
                source_label=source_label,
            )

    return None


def render_markdown(org: str, packages: list[PackageStatus], skipped: int, errors: list[str]) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# {org} PyPI Status",
        "",
        f"Packages published on PyPI whose project metadata points to the {org} GitHub organisation.",
        "",
        "| Package | Latest stable | Newer prereleases | Source |",
        "| --- | --- | --- | --- |",
    ]

    for package in packages:
        prereleases = ", ".join(package.newer_prereleases) if package.newer_prereleases else "-"
        lines.append(
            "| "
            f"[{package.name}]({package.project_url}) | "
            f"{package.latest_stable} | "
            f"{prereleases} | "
            f"[{package.source_label}]({package.source_url}) |"
        )

    if not packages:
        lines.append("| - | - | - | - |")

    lines.extend(
        [
            "",
            f"Generated: {generated_at}",
            "",
            f"Confirmed packages: {len(packages)}",
            f"Repositories without a confirmed PyPI package: {skipped}",
            f"Errors: {len(errors)}",
        ]
    )

    if errors:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {message}" for message in errors)

    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    token = os.environ.get("GH_TOKEN")
    client = HttpClient(token=token, verbose=args.verbose)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    try:
        repos = list_repositories(client, args.org)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to list GitHub repositories for {args.org}: {exc}", file=sys.stderr)
        return 1

    packages: list[PackageStatus] = []
    skipped = 0
    packaging_files = ("pyproject.toml", "setup.cfg", "setup.py", "README.md", "README.rst", "readme.md")

    for repo in repos:
        if args.verbose:
            archived_suffix = " [archived]" if repo.archived else ""
            print(f"Inspecting {repo.name}{archived_suffix}", file=sys.stderr)

        file_contents: dict[str, str] = {}
        try:
            for file_name in packaging_files:
                content = fetch_repository_file(client, args.org, repo, file_name)
                if content is not None:
                    file_contents[file_name] = content
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{repo.name}: failed to fetch repository metadata files ({exc})")
            skipped += 1
            continue

        candidates = extract_candidates(repo, file_contents)
        try:
            status = resolve_package_status(client, repo, candidates, args.verbose)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{repo.name}: failed to resolve PyPI package ({exc})")
            skipped += 1
            continue

        if status is None:
            skipped += 1
            continue

        packages.append(status)

    unique_packages: dict[str, PackageStatus] = {}
    for package in sorted(packages, key=lambda item: item.name.lower()):
        unique_packages.setdefault(normalize_name(package.name), package)

    packages = list(unique_packages.values())
    markdown = render_markdown(args.org, packages, skipped, errors)
    output_path.write_text(markdown, encoding="utf-8")

    print(f"Wrote {output_path}")
    print(f"Confirmed packages: {len(packages)}")
    print(f"Repositories without a confirmed PyPI package: {skipped}")
    print(f"Errors: {len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())