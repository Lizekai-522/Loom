"""Fixed package resources work in wheels without depending on the process CWD."""
from importlib.resources import files
import json


def software_workflow():
    return json.loads(files("loom").joinpath("resources", "software-dev", "workflow-v1.json").read_bytes())


def fixture_manifest():
    # This manifest is explicitly a deterministic demonstration, never a live pack.
    return json.loads(files("loom").joinpath("resources", "software-dev", "manifest.json").read_bytes())
