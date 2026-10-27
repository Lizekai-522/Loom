import os
from pathlib import Path
import tempfile

import pytest

from loom.local_config import PrivateConfigError, load_project_secrets


def private_file(tmp, contents):
    path = Path(tmp) / "secrets.json"
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_secret_loading_does_not_mutate_environment_or_return_mutable_values():
    with tempfile.TemporaryDirectory() as tmp:
        path = private_file(tmp, '{"LOOM_TEST_PRIVATE_KEY":"test-secret-only"}')
        before = dict(os.environ)
        values = load_project_secrets(path, allowed_names={"LOOM_TEST_PRIVATE_KEY"})
        assert values["LOOM_TEST_PRIVATE_KEY"] == "test-secret-only"
        assert "test-secret-only" not in repr(values) + str(values)
        assert dict(os.environ) == before
        with pytest.raises(TypeError):
            values["LOOM_TEST_PRIVATE_KEY"] = "new"


@pytest.mark.parametrize("contents", ['{"PATH":"anything"}', '{"K":"first","K":"second"}',
                                      '{"K":"contains space"}', '["secret"]', '{"K":null}', '{"K":""}'])
def test_invalid_configs_have_static_non_secret_errors(contents):
    with tempfile.TemporaryDirectory() as tmp:
        path = private_file(tmp, contents)
        with pytest.raises(PrivateConfigError, match="^PRIVATE_CONFIG_INVALID$"):
            load_project_secrets(path, allowed_names={"K"})


def test_public_output_directory_is_refused_even_for_ignored_artifacts():
    with pytest.raises(PrivateConfigError):
        load_project_secrets(Path(__file__).resolve(), allowed_names={"K"})
