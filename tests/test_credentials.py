"""Tests for ci_tools.credentials."""
import pytest

from xmsconan.ci_tools.credentials import (
    load_conan_credentials,
    load_credentials,
    read_password_file,
)


def test_load_credentials_from_file(tmp_path):
    """Reads url, username, password from config file."""
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text(
        '[aquapi]\n'
        'url = "https://example.com/"\n'
        'username = "user"\n'
        'password = "pass"\n',
        encoding="utf-8",
    )
    creds = load_credentials(config_path=cfg)

    assert creds["url"] == "https://example.com/"
    assert creds["username"] == "user"
    assert creds["password"] == "pass"


def test_load_credentials_missing_file(tmp_path):
    """Returns empty dict when config file doesn't exist."""
    cfg = tmp_path / ".xmsconan.toml"
    creds = load_credentials(config_path=cfg)

    assert creds == {}


def test_load_credentials_no_aquapi_section(tmp_path):
    """Returns empty dict when [aquapi] section is missing."""
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text('[other]\nkey = "value"\n', encoding="utf-8")
    creds = load_credentials(config_path=cfg)

    assert creds == {}


def test_load_credentials_partial(tmp_path):
    """Returns only the keys present in the config."""
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text('[aquapi]\nurl = "https://x/"\n', encoding="utf-8")
    creds = load_credentials(config_path=cfg)

    assert creds["url"] == "https://x/"
    assert "username" not in creds
    assert "password" not in creds


def test_load_credentials_invalid_toml_raises(tmp_path):
    """A config file that exists but does not parse raises, naming the path.

    Returning {} here made a corrupt config indistinguishable from an absent
    one, so the operator was told to configure ~/.xmsconan.toml -- the file
    they had just mistyped.
    """
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text("this is not valid toml [[[", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Could not parse .*\.xmsconan\.toml"):
        load_credentials(config_path=cfg)


def test_load_conan_credentials_invalid_toml_raises(tmp_path):
    """The [conan] reader reports a malformed config the same way."""
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text("this is not valid toml [[[", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Could not parse .*\.xmsconan\.toml"):
        load_conan_credentials(config_path=cfg)


# --- load_conan_credentials ---


def test_load_conan_credentials_from_file(tmp_path):
    """Reads username and password from [conan] section."""
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text(
        '[conan]\n'
        'username = "conan_user"\n'
        'password = "conan_pass"\n',
        encoding="utf-8",
    )
    creds = load_conan_credentials(config_path=cfg)

    assert creds["username"] == "conan_user"
    assert creds["password"] == "conan_pass"


def test_load_conan_credentials_missing_file(tmp_path):
    """Returns empty dict when config file doesn't exist."""
    cfg = tmp_path / ".xmsconan.toml"
    creds = load_conan_credentials(config_path=cfg)

    assert creds == {}


def test_load_conan_credentials_no_conan_section(tmp_path):
    """Returns empty dict when [conan] section is missing."""
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text('[aquapi]\nurl = "https://x/"\n', encoding="utf-8")
    creds = load_conan_credentials(config_path=cfg)

    assert creds == {}


def test_load_conan_credentials_partial(tmp_path):
    """Returns only the keys present in the [conan] section."""
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text('[conan]\nusername = "user"\n', encoding="utf-8")
    creds = load_conan_credentials(config_path=cfg)

    assert creds["username"] == "user"
    assert "password" not in creds


# --- read_password_file ---


def test_read_password_file_strips_the_editors_newline(tmp_path):
    """The password is the file's content without its trailing whitespace."""
    password_file = tmp_path / "p.txt"
    password_file.write_text("s3cret\n", encoding="utf-8")

    assert read_password_file(str(password_file)) == "s3cret"


def test_load_section_rejects_a_section_that_is_not_a_table(tmp_path):
    """A scalar where a table belongs is named, not left to AttributeError.

    `data.get(section, {})` returns the scalar unchecked, and the caller's next
    `.get` then raised `AttributeError: 'str' object has no attribute 'get'` --
    which names neither the file nor the section. The message reports the type
    and never the value, so a password written on that line cannot reach a log.
    """
    config = tmp_path / ".xmsconan.toml"
    config.write_text('aquapi = "https://example.invalid/"\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"\[aquapi\] must be a table, got str"):
        load_credentials(config)


def test_read_password_file_rejects_a_missing_file(tmp_path):
    """A path naming no file is an error, never a fall-through.

    Both commands that take --password-file resolve further sources after it
    ($CONAN_PASSWORD, then ~/.xmsconan.toml). Returning None here would send a
    typo'd path -- or a file the secret never landed in -- quietly on to
    another account's password, and the login would fail somewhere with no
    mention of the file that was actually asked for.
    """
    with pytest.raises(ValueError, match="password file not found"):
        read_password_file(str(tmp_path / "absent.txt"))


@pytest.mark.parametrize("contents", ["", "   \n"])
def test_read_password_file_rejects_an_empty_file(contents, tmp_path):
    """A file that exists but holds no password is the same error class.

    Separate from the missing-file case because the two do not share a body:
    one writes the file and one deliberately does not, and folding them into
    one parametrized test gave it an `if contents is not None:` -- a branch
    that decides which of two scenarios is running from inside the test.
    """
    password_file = tmp_path / "p.txt"
    password_file.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match="password file is empty"):
        read_password_file(str(password_file))
