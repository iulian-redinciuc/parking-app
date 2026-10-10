"""deploy/scripts/release-notes.sh: release notes from the commit messages (deployment.md §8)."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[3] / "deploy" / "scripts" / "release-notes.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, subject: str) -> None:
    _git(repo, "commit", "--allow-empty", "-m", subject)


def _notes(repo: Path, tag: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPT), tag], cwd=repo, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "Initial plan")
    _commit(tmp_path, "P0.1: repo layout")
    _commit(tmp_path, "P10.2: a later phase")
    _commit(tmp_path, "P2.3: ingest")
    _commit(tmp_path, "fix CI: cache key")
    _commit(tmp_path, "P2.4: smoothing")
    _git(tmp_path, "tag", "v0.1.0")
    _commit(tmp_path, "P8.4: compose hardening")
    _git(tmp_path, "tag", "v0.2.0")
    return tmp_path


def test_first_release_lists_the_whole_history_grouped_by_phase(repo: Path) -> None:
    out = _notes(repo, "v0.1.0").stdout
    assert "docker pull ghcr.io/iulian-redinciuc/parking-api:v0.1.0" in out
    assert "docker pull ghcr.io/iulian-redinciuc/parking-vision:v0.1.0" in out
    assert "PARKING_VERSION=v0.1.0" in out
    assert "## Changes\n" in out
    headings = [line for line in out.splitlines() if line.startswith("### ")]
    assert headings == ["### Phase 0", "### Phase 2", "### Phase 10", "### Other"]
    phase2 = out.split("### Phase 2\n")[1].split("###")[0].strip().splitlines()
    assert [line.rsplit(" (", 1)[0] for line in phase2] == ["- P2.3: ingest", "- P2.4: smoothing"]
    other = out.split("### Other\n")[1].strip().splitlines()
    assert [line.rsplit(" (", 1)[0] for line in other] == ["- Initial plan", "- fix CI: cache key"]
    assert "P8.4" not in out


def test_later_release_lists_only_commits_since_the_previous_tag(repo: Path) -> None:
    out = _notes(repo, "v0.2.0").stdout
    assert "## Changes since v0.1.0" in out
    assert [line for line in out.splitlines() if line.startswith("- ")][0].startswith("- P8.4: ")
    assert out.count("\n- ") == 1


def test_unknown_tag_fails(repo: Path) -> None:
    result = _notes(repo, "v9.9.9")
    assert result.returncode != 0
    assert "unknown tag" in result.stderr
