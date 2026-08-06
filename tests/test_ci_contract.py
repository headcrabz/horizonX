"""Guard the dependency contract used by the full test workflow."""

from pathlib import Path


def test_full_test_job_installs_dashboard_test_dependencies() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text()
    test_job, _ = workflow.split("  lint-only:", maxsplit=1)

    assert 'pip install -e ".[dev,dashboard]"' in test_job
