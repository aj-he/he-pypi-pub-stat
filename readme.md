# HistoricEngland PyPI Status

This repository contains a small Python script that discovers packages published on PyPI whose project metadata points at the HistoricEngland GitHub organisation, then writes a wiki-friendly `status.md` file.

## What the script reports

The generated `status.md` contains one row per confirmed package with these columns:

- Package name linked to the PyPI project page
- Latest stable version published on PyPI
- Any prerelease versions newer than the latest stable version
- Source repository link in the HistoricEngland GitHub organisation

Packages are only included when the script can confirm both of these conditions:

- The package exists on PyPI
- The PyPI project metadata includes a HistoricEngland GitHub URL in `project_urls` or `home_page`

## Requirements

- Python 3.11 or newer
- Optional `GH_TOKEN` environment variable for higher GitHub API rate limits

If you use a GitHub token, a fine-grained token with `Metadata` and `Contents` read access to the relevant HistoricEngland repositories is sufficient.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Usage

```bash
python scripts/update_status.py
```

Useful options:

```bash
python scripts/update_status.py --verbose
python scripts/update_status.py --output status.md
python scripts/update_status.py --org HistoricEngland
```

The script overwrites `status.md` on each run.

## Output format

The generated Markdown is intended to be copied directly into an Azure DevOps wiki page. It uses a standard Markdown table and adds a generation timestamp plus summary counts.

## Notes

- Repository discovery starts from the GitHub organisation repositories list.
- Package name discovery checks common Python packaging files at the repository root: `pyproject.toml`, `setup.cfg`, `setup.py`, and common README variants.
- When a repository name does not match the PyPI project name, the package can still be included as long as the PyPI metadata points back to HistoricEngland.
