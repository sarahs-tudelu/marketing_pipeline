import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from social import reference_grounding as refs
from social.facts import load_facts


def test_reference_configuration_does_not_depend_on_working_directory(db, settings, tmp_path, monkeypatch):
    sheet = load_facts(db, settings.facts_path)
    assert not sheet.document.get("visual_guidance")
    monkeypatch.chdir(tmp_path)
    guidance = refs.builder_guidance(db, SimpleNamespace(brief={"facts_id": sheet.id}))
    assert any(p.product_id == "canopies" for p in guidance.products)
    assert refs.reference_config("website-product-references.yaml")["canopies"]["images"]


def test_missing_reference_resource_explains_build_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(refs, "files", lambda _: tmp_path)
    with pytest.raises(ValueError, match="missing from this application build") as error:
        refs.reference_config("website-product-references.yaml")
    assert "Rebuild web and worker" in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_built_wheel_contains_and_loads_reference_data_away_from_checkout(tmp_path):
    pytest.importorskip("setuptools", reason="Install the pyproject build requirements to test the wheel")
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(root / "pyproject.toml", project / "pyproject.toml")
    shutil.copytree(root / "social", project / "social", ignore=shutil.ignore_patterns("__pycache__"))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    built = subprocess.run(
        [sys.executable, "-c", "from setuptools.build_meta import build_wheel; "
         "import sys; build_wheel(sys.argv[1])", str(wheels)],
        cwd=project, capture_output=True, text=True, timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    installed = tmp_path / "site-packages"
    with zipfile.ZipFile(next(wheels.glob("*.whl"))) as wheel:
        for name in ("visual-guidance.builder.yaml", "website-product-references.yaml"):
            resource = "social/resources/" + name
            assert wheel.read(resource) == (root / resource).read_bytes()
        # Pure Python wheels install these files directly into site-packages.
        wheel.extractall(installed)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    checked = subprocess.run(
        [sys.executable, "-I", "-c", """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from social import reference_grounding as refs
from social.product_rules import VisualGuidance
assert Path(refs.__file__).is_relative_to(Path(sys.argv[1]))
guidance = VisualGuidance.model_validate(refs.reference_config('visual-guidance.builder.yaml'))
catalog = refs.reference_config('website-product-references.yaml')
assert any(p.product_id == 'canopies' for p in guidance.products)
assert catalog['canopies']['images']
print(json.dumps({'products': len(guidance.products), 'website_families': sorted(catalog)}))
""", str(installed)], cwd=unrelated, capture_output=True, text=True, timeout=60,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert json.loads(checked.stdout)["products"] == 7
