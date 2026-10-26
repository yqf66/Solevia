"""Install the shipped Solevia skills into the isolated agent workspace."""

import shutil
from pathlib import Path


def prepare_workspace(workspace: Path):
    workspace.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[4] / "skills"
    for skill in source.glob("*/SKILL.md"):
        target = workspace / ".agents" / "skills" / skill.parent.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill, target / "SKILL.md")
