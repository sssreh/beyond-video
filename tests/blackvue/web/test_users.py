from pathlib import Path

import pytest

from blackvue.web.users import UsersConfig
from blackvue.web.users import UsersConfigError
from blackvue.web.users import hash_password
from blackvue.web.users import load_users_config
from blackvue.web.users import save_users_config
from blackvue.web.users import validate_role
from blackvue.web.users import verify_password


def test_hash_password_round_trip():
    hashed = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password", hashed) is False


def test_hash_password_uses_a_fresh_salt_each_time():
    first = hash_password("same password")
    second = hash_password("same password")

    assert first != second
    assert verify_password("same password", first) is True
    assert verify_password("same password", second) is True


def test_verify_password_rejects_malformed_stored_value():
    assert verify_password("anything", "not-a-real-hash") is False
    assert verify_password("anything", "") is False


def test_validate_role_accepts_known_roles():
    for role in ["owner", "viewer"]:
        validate_role(role)


def test_validate_role_rejects_unknown_role():
    with pytest.raises(UsersConfigError):
        validate_role("manager")


def test_add_user_and_authenticate():
    config = UsersConfig(path=Path("/tmp/does-not-matter.cfg"))

    config.add_user("christer", "hunter2", "owner")

    user = config.authenticate("christer", "hunter2")
    assert user is not None
    assert user.username == "christer"
    assert user.role == "owner"

    assert config.authenticate("christer", "wrong") is None
    assert config.authenticate("nobody", "hunter2") is None


def test_add_user_rejects_duplicate_username():
    config = UsersConfig(path=Path("/tmp/does-not-matter.cfg"))
    config.add_user("christer", "hunter2", "owner")

    with pytest.raises(UsersConfigError):
        config.add_user("christer", "different", "viewer")


def test_add_user_rejects_bad_role():
    config = UsersConfig(path=Path("/tmp/does-not-matter.cfg"))

    with pytest.raises(UsersConfigError):
        config.add_user("someone", "password", "manager")


def test_add_user_rejects_empty_username_or_password():
    config = UsersConfig(path=Path("/tmp/does-not-matter.cfg"))

    with pytest.raises(UsersConfigError):
        config.add_user("", "password", "viewer")

    with pytest.raises(UsersConfigError):
        config.add_user("someone", "", "viewer")


def test_remove_user():
    config = UsersConfig(path=Path("/tmp/does-not-matter.cfg"))
    config.add_user("christer", "hunter2", "owner")

    config.remove_user("christer")

    assert config.get("christer") is None


def test_remove_user_raises_for_unknown_username():
    config = UsersConfig(path=Path("/tmp/does-not-matter.cfg"))

    with pytest.raises(UsersConfigError):
        config.remove_user("nobody")


def test_load_missing_file_returns_zero_users(tmp_path):
    config = load_users_config(tmp_path / "web-users.cfg")

    assert config.users == {}


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "web-users.cfg"
    config = UsersConfig(path=path)
    config.add_user("christer", "hunter2", "owner")
    config.add_user("family", "sommaren26", "viewer")

    config.save()

    loaded = load_users_config(path)

    assert set(loaded.users) == {"christer", "family"}
    assert loaded.get("christer").role == "owner"
    assert loaded.get("family").role == "viewer"
    assert loaded.authenticate("christer", "hunter2") is not None
    assert loaded.authenticate("family", "wrong") is None


def test_load_raises_on_bad_toml(tmp_path):
    path = tmp_path / "web-users.cfg"
    path.write_text("this is not [ valid toml")

    with pytest.raises(UsersConfigError):
        load_users_config(path)


def test_load_raises_on_missing_required_key(tmp_path):
    path = tmp_path / "web-users.cfg"
    path.write_text('[[user]]\nusername = "christer"\nrole = "owner"\n')

    with pytest.raises(UsersConfigError):
        load_users_config(path)


def test_load_raises_on_unknown_role(tmp_path):
    path = tmp_path / "web-users.cfg"
    path.write_text(
        '[[user]]\nusername = "christer"\n'
        'password_hash = "pbkdf2_sha256$1$00$00"\n'
        'role = "manager"\n'
    )

    with pytest.raises(UsersConfigError):
        load_users_config(path)


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "web-users.cfg"
    config = UsersConfig(path=path)
    config.add_user("christer", "hunter2", "owner")

    save_users_config(path, config)

    assert path.is_file()
