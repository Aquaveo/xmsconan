"""Tests for ci_tools.credentials."""
from xmsconan.ci_tools.credentials import load_conan_credentials, load_credentials


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


def test_load_credentials_invalid_toml(tmp_path):
    """Returns empty dict on malformed TOML."""
    cfg = tmp_path / ".xmsconan.toml"
    cfg.write_text("this is not valid toml [[[", encoding="utf-8")
    creds = load_credentials(config_path=cfg)

    assert creds == {}


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
