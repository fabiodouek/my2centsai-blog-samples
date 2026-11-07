"""Unit tests for secrets_env.py, on throwaway files (the real secrets file is never read here).

    uv run --no-project --with pytest pytest test_secrets_env.py -v
"""

import io

import pytest

import secrets_env


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    monkeypatch.setenv("OSB_SECRETS_ENV", str(path))
    return path


def test_parses_plain_export_and_quoted_values(secrets):
    secrets.write_text("# comment\nAWS_TEST_BUCKET=first\nexport AWS_TEST_BUCKET='my-bucket'  \nOTHER_KEY=x\n")
    assert secrets_env.read_secret("AWS_TEST_BUCKET") == "my-bucket"  # the last assignment wins
    secrets.write_text('AWS_TEST_BUCKET="quoted # not a comment"\n')
    assert secrets_env.read_secret("AWS_TEST_BUCKET") == "quoted # not a comment"
    secrets.write_text("AWS_TEST_BUCKET=plain # a comment\n")
    assert secrets_env.read_secret("AWS_TEST_BUCKET") == "plain"


def test_nothing_is_expanded_or_run(secrets, tmp_path):
    marker = tmp_path / "ran"
    secrets.write_text(f"AWS_TEST_BUCKET=$(touch {marker})\n")
    assert secrets_env.read_secret("AWS_TEST_BUCKET") == f"$(touch {marker})"
    assert not marker.exists()


def test_only_allowed_keys(secrets):
    secrets.write_text("SOME_API_KEY=value-not-for-part-5\n")
    with pytest.raises(KeyError) as caught:
        secrets_env.read_secret("SOME_API_KEY")
    assert "value-not-for-part-5" not in str(caught.value)


def test_missing_key_and_missing_file(secrets):
    secrets.write_text("AWS_TEST_BUCKET=\n")
    with pytest.raises(KeyError):
        secrets_env.read_secret("AWS_TEST_BUCKET")
    secrets.unlink()
    with pytest.raises(FileNotFoundError):
        secrets_env.read_secret("AWS_TEST_BUCKET")


def test_default_path_is_under_home_and_messages_show_tilde(monkeypatch, tmp_path):
    monkeypatch.delenv("OSB_SECRETS_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert secrets_env.secrets_path() == tmp_path / ".config" / "opensandbox-eks" / "secrets.env"
    with pytest.raises(FileNotFoundError) as caught:
        secrets_env.read_secret("AWS_TEST_BUCKET")
    assert "~/.config/opensandbox-eks/secrets.env" in str(caught.value) and str(tmp_path) not in str(caught.value)


class FakeStdout(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def test_main_prints_into_a_pipe_but_not_to_a_terminal(secrets, monkeypatch, capsys):
    secrets.write_text("AWS_TEST_BUCKET=my-bucket\n")
    pipe = FakeStdout(tty=False)
    monkeypatch.setattr("sys.stdout", pipe)
    assert secrets_env.main(["secrets_env.py", "AWS_TEST_BUCKET"]) == 0
    assert pipe.getvalue() == "my-bucket"
    terminal = FakeStdout(tty=True)
    monkeypatch.setattr("sys.stdout", terminal)
    assert secrets_env.main(["secrets_env.py", "AWS_TEST_BUCKET"]) == 2
    assert terminal.getvalue() == ""
    assert "my-bucket" not in capsys.readouterr().err


def test_quoted_value_followed_by_a_comment(secrets):
    secrets.write_text('AWS_TEST_BUCKET="my-bucket" # the test bucket\n')
    assert secrets_env.read_secret("AWS_TEST_BUCKET") == "my-bucket"
    secrets.write_text("AWS_TEST_BUCKET='my-bucket'   # single quotes too\n")
    assert secrets_env.read_secret("AWS_TEST_BUCKET") == "my-bucket"
    secrets.write_text('AWS_TEST_BUCKET="unterminated\n')  # no closing quote: taken as it is
    assert secrets_env.read_secret("AWS_TEST_BUCKET") == '"unterminated'
