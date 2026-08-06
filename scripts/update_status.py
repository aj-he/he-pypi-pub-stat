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
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_RAW_ROOT = "https://raw.githubusercontent.com"
PYPI_API_TEMPLATE = "https://pypi.org/pypi/{name}/json"
ORG_URL_PATTERN = re.compile(r"https?://github\.com/HistoricEngland/[^\s)\]>'\"]+", re.IGNORECASE)
PYPI_PROJECT_PATTERN = re.compile(r"https?://pypi\.org/project/([^/]+)/?", re.IGNORECASE)
PYPI_BADGE_PATTERN = re.compile(r"(?:img\.shields\.io|badge\.fury\.io)/(?:pypi|py)/v/([^\s)\]>'\"]+)", re.IGNORECASE)
SETUP_NAME_PATTERN = re.compile(r"name\s*=\s*[\"']([^\"']+)[\"']")
INTERNAL_GITHUB_REPO_PATTERN = re.compile(r"github\.com[/:]HistoricEngland/([A-Za-z0-9._-]+?)(?:\.git)?(?:[/?#@]|$)", re.IGNORECASE)
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
    source_repo_name: str
    arches_version: str


@dataclass(frozen=True)
class RepoInspection:
    repo: Repo
    arches_version: str
    dependencies: dict[str, str]
    status: PackageStatus | None


@dataclass(frozen=True)
class DependencyUse:
    consumer_name: str
    consumer_url: str
    requirement: str
    is_package: bool


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


def format_poetry_dependency_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("version"), str):
            return value["version"]
        details: list[str] = []
        for key in ("git", "url", "path", "branch", "tag", "rev"):
            key_value = value.get(key)
            if isinstance(key_value, str) and key_value:
                details.append(f"{key}={key_value}")
        if details:
            return ", ".join(details)
    return None


def extract_arches_version_from_pyproject(pyproject_text: str | None) -> str:
    if not pyproject_text:
        return "-"

    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return "-"

    if not isinstance(data, dict):
        return "-"

    project = data.get("project")
    if isinstance(project, dict):
        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if not isinstance(dependency, str):
                    continue
                try:
                    requirement = Requirement(dependency)
                except InvalidRequirement:
                    continue
                if normalize_name(requirement.name) == "arches":
                    return dependency.strip()

    tool = data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            poetry_dependencies = poetry.get("dependencies")
            if isinstance(poetry_dependencies, dict):
                for dependency_name, dependency_value in poetry_dependencies.items():
                    if isinstance(dependency_name, str) and normalize_name(dependency_name) == "arches":
                        return format_poetry_dependency_value(dependency_value) or "-"

    return "-"


def describe_requirement(requirement: Requirement) -> str:
    if requirement.url:
        detail = requirement.url
    elif requirement.specifier:
        detail = str(requirement.specifier)
    else:
        detail = "any"

    if requirement.marker:
        detail = f"{detail}; {requirement.marker}"
    return detail


def add_dependency(dependencies: dict[str, str], name: str, requirement_text: str) -> None:
    dependencies.setdefault(normalize_name(name), requirement_text)


def parse_requirement_text(requirement_text: str) -> Requirement | None:
    try:
        return Requirement(requirement_text)
    except InvalidRequirement:
        return None


def infer_internal_repo_name(requirement_text: str) -> str | None:
    match = INTERNAL_GITHUB_REPO_PATTERN.search(requirement_text)
    if not match:
        return None
    return match.group(1)


def extract_dependencies_from_pyproject(pyproject_text: str | None) -> dict[str, str]:
    if not pyproject_text:
        return {}

    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}

    dependencies: dict[str, str] = {}
    project = data.get("project")
    if isinstance(project, dict):
        project_dependencies = project.get("dependencies")
        if isinstance(project_dependencies, list):
            for dependency in project_dependencies:
                if not isinstance(dependency, str):
                    continue
                requirement = parse_requirement_text(dependency)
                if requirement is None:
                    continue
                add_dependency(dependencies, requirement.name, describe_requirement(requirement))

    tool = data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            poetry_dependencies = poetry.get("dependencies")
            if isinstance(poetry_dependencies, dict):
                for dependency_name, dependency_value in poetry_dependencies.items():
                    if not isinstance(dependency_name, str) or normalize_name(dependency_name) == "python":
                        continue
                    dependency_text = format_poetry_dependency_value(dependency_value)
                    if dependency_text:
                        add_dependency(dependencies, dependency_name, dependency_text)

    return dependencies


def extract_dependencies_from_setup_cfg(setup_cfg_text: str | None) -> dict[str, str]:
    if not setup_cfg_text:
        return {}

    parser = configparser.ConfigParser()
    try:
        parser.read_string(setup_cfg_text)
    except configparser.Error:
        return {}

    if not parser.has_option("options", "install_requires"):
        return {}

    dependencies: dict[str, str] = {}
    for raw_line in parser.get("options", "install_requires").splitlines():
        dependency = raw_line.strip()
        if not dependency:
            continue
        requirement = parse_requirement_text(dependency)
        if requirement is not None:
            add_dependency(dependencies, requirement.name, describe_requirement(requirement))
            continue
        internal_repo_name = infer_internal_repo_name(dependency)
        if internal_repo_name:
            add_dependency(dependencies, internal_repo_name, dependency)

    return dependencies


def extract_dependencies_from_requirements(requirements_text: str | None) -> dict[str, str]:
    if not requirements_text:
        return {}

    dependencies: dict[str, str] = {}
    for raw_line in requirements_text.splitlines():
        dependency = raw_line.split("#", 1)[0].strip()
        if not dependency or dependency.startswith(("-", "--")):
            continue
        requirement = parse_requirement_text(dependency)
        if requirement is not None:
            add_dependency(dependencies, requirement.name, describe_requirement(requirement))
            continue
        internal_repo_name = infer_internal_repo_name(dependency)
        if internal_repo_name:
            add_dependency(dependencies, internal_repo_name, dependency)

    return dependencies


def extract_dependencies(file_contents: dict[str, str]) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for extracted in (
        extract_dependencies_from_pyproject(file_contents.get("pyproject.toml")),
        extract_dependencies_from_setup_cfg(file_contents.get("setup.cfg")),
        extract_dependencies_from_requirements(file_contents.get("requirements.txt")),
        extract_dependencies_from_requirements(file_contents.get("requirements-dev.txt")),
        extract_dependencies_from_requirements(file_contents.get("requirements_dev.txt")),
        extract_dependencies_from_requirements(file_contents.get("dev-requirements.txt")),
    ):
        for dependency_name, requirement_text in extracted.items():
            dependencies.setdefault(dependency_name, requirement_text)
    return dependencies


def resolve_package_status(
    client: HttpClient,
    repo: Repo,
    candidates: Iterable[Candidate],
    arches_version: str,
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
                source_repo_name=repo.name,
                arches_version=arches_version,
            )

    return None


def format_consumer_links(consumers: list[DependencyUse]) -> str:
    if not consumers:
        return "-"

    return ", ".join(
        f"[{consumer.consumer_name}]({consumer.consumer_url}) ({consumer.requirement})"
        for consumer in consumers
    )


def build_dependency_uses(
    packages: list[PackageStatus], inspections: list[RepoInspection]
) -> dict[str, list[DependencyUse]]:
    package_names = {normalize_name(package.name) for package in packages}
    package_repo_names = {package.source_repo_name for package in packages}
    dependency_uses: dict[str, list[DependencyUse]] = {normalize_name(package.name): [] for package in packages}

    for inspection in inspections:
        is_package = inspection.repo.name in package_repo_names
        consumer_name = inspection.status.name if inspection.status is not None else inspection.repo.name
        for dependency_name, requirement_text in sorted(inspection.dependencies.items()):
            if dependency_name not in package_names:
                continue
            dependency_uses[dependency_name].append(
                DependencyUse(
                    consumer_name=consumer_name,
                    consumer_url=inspection.repo.html_url,
                    requirement=requirement_text,
                    is_package=is_package,
                )
            )

    for dependency_name, consumers in dependency_uses.items():
        dependency_uses[dependency_name] = sorted(
            consumers,
            key=lambda item: (item.is_package, item.consumer_name.lower(), item.requirement),
        )

    return dependency_uses


def make_mermaid_id(prefix: str, label: str) -> str:
    sanitized = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "node"
    return f"{prefix}_{sanitized}"


def escape_mermaid_label(label: str) -> str:
    return label.replace('"', "'")


def render_dependency_graph(packages: list[PackageStatus], inspections: list[RepoInspection]) -> str | None:
    package_by_name = {normalize_name(package.name): package for package in packages}
    package_repo_names = {package.source_repo_name for package in packages}
    package_by_repo = {package.source_repo_name: package for package in packages}
    internal_by_dependency_name: dict[str, tuple[str, str]] = {}
    for inspection in inspections:
        internal_by_dependency_name.setdefault(normalize_name(inspection.repo.name), (inspection.repo.name, "component"))
        internal_by_dependency_name.setdefault(
            normalize_name(inspection.repo.name.replace("-", "_")),
            (inspection.repo.name, "component"),
        )
        internal_by_dependency_name.setdefault(
            normalize_name(inspection.repo.name.replace("_", "-")),
            (inspection.repo.name, "component"),
        )
        if inspection.status is not None:
            package = inspection.status
            internal_by_dependency_name.setdefault(normalize_name(package.name), (package.name, "package"))
    edges: list[tuple[str, str, str]] = []
    node_labels: dict[str, str] = {}
    node_classes: dict[str, str] = {}

    # Always include confirmed PyPI packages, even if they are not connected.
    for package in packages:
        package_id = make_mermaid_id("pkg", package.name)
        node_labels.setdefault(package_id, package.name)
        node_classes[package_id] = "package"

    for inspection in inspections:
        consumer_is_package = inspection.repo.name in package_repo_names
        consumer_label = inspection.status.name if inspection.status is not None else inspection.repo.name
        consumer_id = make_mermaid_id("pkg" if consumer_is_package else "app", consumer_label)

        for dependency_name, requirement_text in sorted(inspection.dependencies.items()):
            target_label: str | None = None
            target_class: str | None = None
            package = package_by_name.get(dependency_name)
            if package is not None:
                target_label = package.name
                target_class = "package"
            else:
                internal_target = internal_by_dependency_name.get(dependency_name)
                if internal_target is None:
                    internal_repo_name = infer_internal_repo_name(requirement_text)
                    if internal_repo_name:
                        internal_target = internal_by_dependency_name.get(normalize_name(internal_repo_name))
                if internal_target is not None:
                    target_label = internal_target[0]
                    target_class = internal_target[1]

            if target_label is None or target_class is None:
                continue

            target_id = make_mermaid_id("pkg" if target_class == "package" else "cmp", target_label)
            if consumer_id == target_id:
                continue

            node_labels.setdefault(consumer_id, consumer_label)
            node_classes[consumer_id] = "package" if consumer_is_package else "application"
            node_labels.setdefault(target_id, target_label)
            node_classes[target_id] = target_class
            edges.append((consumer_id, target_id, requirement_text))

    if not node_labels:
        return None

    package_ids = sorted(node_id for node_id, class_name in node_classes.items() if class_name == "package")
    application_ids = sorted(node_id for node_id, class_name in node_classes.items() if class_name == "application")
    component_ids = sorted(node_id for node_id, class_name in node_classes.items() if class_name == "component")

    if edges:
        connected_ids: set[str] = set()
        for source_id, target_id, _ in edges:
            connected_ids.add(source_id)
            connected_ids.add(target_id)
        # Keep all packages, but prune unconnected applications/components.
        keep_ids = connected_ids | set(package_ids)
        node_labels = {node_id: label for node_id, label in node_labels.items() if node_id in keep_ids}
        node_classes = {node_id: class_name for node_id, class_name in node_classes.items() if node_id in keep_ids}
        package_ids = sorted(node_id for node_id, class_name in node_classes.items() if class_name == "package")
        application_ids = sorted(node_id for node_id, class_name in node_classes.items() if class_name == "application")
        component_ids = sorted(node_id for node_id, class_name in node_classes.items() if class_name == "component")
    else:
        # No edges: only render standalone package nodes.
        node_labels = {node_id: label for node_id, label in node_labels.items() if node_classes.get(node_id) == "package"}
        node_classes = {node_id: class_name for node_id, class_name in node_classes.items() if class_name == "package"}
        package_ids = sorted(node_labels.keys())
        application_ids = []
        component_ids = []

    lines = ["```mermaid", "flowchart LR"]
    for node_id in sorted(node_labels):
        lines.append(f'  {node_id}["{escape_mermaid_label(node_labels[node_id])}"]')
    for source_id, target_id, requirement_text in edges:
        lines.append(f'  {source_id} -->|"{escape_mermaid_label(requirement_text)}"| {target_id}')
    lines.append("  classDef package fill:#e8f0ff,stroke:#3766b1,color:#10223f;")
    lines.append("  classDef application fill:#edf8ec,stroke:#467a49,color:#18321a;")
    lines.append("  classDef component fill:#fff6e5,stroke:#a06a00,color:#3f2a00;")
    if package_ids:
        lines.append(f"  class {','.join(package_ids)} package;")
    if application_ids:
        lines.append(f"  class {','.join(application_ids)} application;")
    if component_ids:
        lines.append(f"  class {','.join(component_ids)} component;")
    lines.append("```")
    return "\n".join(lines)


def render_markdown(
    org: str,
    packages: list[PackageStatus],
    dependency_uses: dict[str, list[DependencyUse]],
    dependency_graph: str | None,
    skipped: int,
    errors: list[str],
) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    application_dependency_count = sum(
        1
        for consumers in dependency_uses.values()
        if any(not consumer.is_package for consumer in consumers)
    )
    internal_edge_count = sum(len(consumers) for consumers in dependency_uses.values())
    lines = [
        f"# {org} PyPI Status",
        "",
        f"Packages published on PyPI whose project metadata points to the {org} GitHub organisation.",
        "",
        "| Package | Latest stable | Newer prereleases | Arches (pyproject) | Applications using package | Source |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for package in packages:
        prereleases = ", ".join(package.newer_prereleases) if package.newer_prereleases else "-"
        applications = [consumer for consumer in dependency_uses.get(normalize_name(package.name), []) if not consumer.is_package]
        lines.append(
            "| "
            f"[{package.name}]({package.project_url}) | "
            f"{package.latest_stable} | "
            f"{prereleases} | "
            f"{package.arches_version} | "
            f"{format_consumer_links(applications)} | "
            f"[{package.source_label}]({package.source_url}) |"
        )

    if not packages:
        lines.append("| - | - | - | - | - | - |")

    lines.extend(
        [
            "",
            f"Generated: {generated_at}",
            "",
            f"Confirmed packages: {len(packages)}",
            f"Packages used by at least one application: {application_dependency_count}",
            f"Internal dependency edges found: {internal_edge_count}",
            f"Repositories without a confirmed PyPI package: {skipped}",
            f"Errors: {len(errors)}",
        ]
    )

    if dependency_graph:
        lines.extend(["", "## Dependency graph", "", dependency_graph])

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
    inspections: list[RepoInspection] = []
    skipped = 0
    packaging_files = (
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "README.md",
        "README.rst",
        "readme.md",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements_dev.txt",
        "dev-requirements.txt",
    )

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
        arches_version = extract_arches_version_from_pyproject(file_contents.get("pyproject.toml"))
        dependencies = extract_dependencies(file_contents)
        try:
            status = resolve_package_status(client, repo, candidates, arches_version, args.verbose)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{repo.name}: failed to resolve PyPI package ({exc})")
            skipped += 1
            continue

        inspections.append(
            RepoInspection(
                repo=repo,
                arches_version=arches_version,
                dependencies=dependencies,
                status=status,
            )
        )

        if status is None:
            skipped += 1
            continue

        packages.append(status)

    unique_packages: dict[str, PackageStatus] = {}
    for package in sorted(packages, key=lambda item: item.name.lower()):
        unique_packages.setdefault(normalize_name(package.name), package)

    packages = list(unique_packages.values())
    dependency_uses = build_dependency_uses(packages, inspections)
    dependency_graph = render_dependency_graph(packages, inspections)
    markdown = render_markdown(args.org, packages, dependency_uses, dependency_graph, skipped, errors)
    output_path.write_text(markdown, encoding="utf-8")

    print(f"Wrote {output_path}")
    print(f"Confirmed packages: {len(packages)}")
    print(f"Repositories without a confirmed PyPI package: {skipped}")
    print(f"Errors: {len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())