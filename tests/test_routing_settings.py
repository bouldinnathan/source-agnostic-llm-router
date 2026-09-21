"""Dashboard routing settings: validation and private, atomic persistence."""

from __future__ import annotations

import json
import os
import stat

import pytest

from llm_router.routing_settings import (
    MAX_RACE_EVERY, MIN_RACE_EVERY, RoutingSettings, RoutingSettingsStore, validate_settings,
)


def test_defaults_keep_current_behaviour_and_missing_file_reads_as_defaults(tmp_path):
    store = RoutingSettingsStore(tmp_path / "missing" / "routing-settings.json")
    assert store.load() == RoutingSettings()
    assert RoutingSettings().to_dict() == {
        "advertise_machine_aliases": True, "prefer_fastest_replica": False, "prefer_first_token": False,
        "race_replicas": False, "race_every": 20,
        "first_token_timeout_seconds": 300, "idle_timeout_seconds": 90, "max_request_seconds": 0,
    }
    assert not (tmp_path / "missing").exists(), "Reading never creates files"


def test_default_path_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_ROUTER_ROUTING_SETTINGS_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert RoutingSettingsStore().path == tmp_path / "config" / "llm-router" / "routing-settings.json"
    monkeypatch.setenv("LLM_ROUTER_ROUTING_SETTINGS_FILE", str(tmp_path / "custom" / "settings.json"))
    assert RoutingSettingsStore().path == tmp_path / "custom" / "settings.json"


@pytest.mark.parametrize("data,expected", [
    ({}, RoutingSettings()),
    ({"advertise_machine_aliases": False}, RoutingSettings(advertise_machine_aliases=False)),
    ({"race_replicas": True, "race_every": 5}, RoutingSettings(race_replicas=True, race_every=5)),
    ({"race_every": 7.0}, RoutingSettings(race_every=7)),
])
def test_partial_updates_apply_onto_a_base(data, expected):
    assert validate_settings(data) == expected
    base = RoutingSettings(prefer_fastest_replica=True)
    merged = validate_settings(data, base=base)
    assert merged.prefer_fastest_replica is True or "prefer_fastest_replica" in data


@pytest.mark.parametrize("data", [
    "not an object", ["list"], {"unknown": True}, {"advertise_machine_aliases": "yes"},
    {"prefer_fastest_replica": 1}, {"race_replicas": None}, {"race_every": True},
    {"race_every": MIN_RACE_EVERY - 1}, {"race_every": MAX_RACE_EVERY + 1}, {"race_every": 2.5},
    {"race_every": "20"}, {"race_every": float("nan")},
])
def test_invalid_settings_are_rejected(data):
    with pytest.raises(ValueError):
        validate_settings(data)


def test_save_and_load_round_trip_with_private_files(tmp_path):
    path = tmp_path / "state" / "routing-settings.json"
    store = RoutingSettingsStore(path)
    saved = store.save(RoutingSettings(advertise_machine_aliases=False, race_replicas=True, race_every=3))
    assert saved == RoutingSettings(advertise_machine_aliases=False, race_replicas=True, race_every=3)
    assert RoutingSettingsStore(path).load() == saved
    assert json.loads(path.read_text()) == {"version": 1, "settings": saved.to_dict()}
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.save(RoutingSettings())
    assert RoutingSettingsStore(path).load() == RoutingSettings()


@pytest.mark.parametrize("contents", [
    b"not json", b"[]", b'{"version": 2, "settings": {}}', b'{"version": 1}',
    b'{"version": 1, "settings": {"race_every": 0}}', b'{"version": 1, "settings": {"extra": 1}}',
    b'{"version": 1, "settings": {}, "settings": {}}',
])
def test_corrupt_or_unknown_files_are_reported_and_never_replaced(tmp_path, contents):
    path = tmp_path / "routing-settings.json"
    path.write_bytes(contents)
    path.chmod(0o600)
    store = RoutingSettingsStore(path)
    with pytest.raises(RuntimeError) as error:
        store.load()
    assert str(path) not in str(error.value)
    with pytest.raises(RuntimeError):
        store.save(RoutingSettings())
    assert path.read_bytes() == contents


def test_symlinked_or_shared_files_are_refused(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"version": 1, "settings": {}}))
    target.chmod(0o600)
    link = tmp_path / "routing-settings.json"
    os.symlink(target, link)
    with pytest.raises(RuntimeError):
        RoutingSettingsStore(link).load()
    with pytest.raises(RuntimeError):
        RoutingSettingsStore(link).save(RoutingSettings())
    assert os.path.islink(link) and target.read_text()
    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps({"version": 1, "settings": {}}))
    shared.chmod(0o644)
    with pytest.raises(RuntimeError):
        RoutingSettingsStore(shared).load()


def test_oversized_file_is_refused(tmp_path):
    path = tmp_path / "routing-settings.json"
    path.write_bytes(b" " * 9000 + b"{}")
    path.chmod(0o600)
    with pytest.raises(RuntimeError):
        RoutingSettingsStore(path).load()


@pytest.mark.parametrize("data,expected", [
    ({"first_token_timeout_seconds": 600}, 600), ({"first_token_timeout_seconds": 5.0}, 5),
])
def test_timeout_fields_accept_whole_seconds(data, expected):
    assert validate_settings(data).first_token_timeout_seconds == expected
    assert validate_settings({"idle_timeout_seconds": 3600}).idle_timeout_seconds == 3600
    assert validate_settings({"max_request_seconds": 0}).max_request_seconds == 0
    assert validate_settings({"max_request_seconds": 86400}).max_request_seconds == 86400
    assert validate_settings({"prefer_first_token": True}).prefer_first_token is True


@pytest.mark.parametrize("data", [
    {"first_token_timeout_seconds": 4}, {"first_token_timeout_seconds": 3601}, {"idle_timeout_seconds": 0},
    {"max_request_seconds": -1}, {"max_request_seconds": 86401}, {"max_request_seconds": "none"},
    {"prefer_first_token": "yes"}, {"first_token_timeout_seconds": True},
])
def test_timeout_fields_reject_out_of_range_values(data):
    with pytest.raises(ValueError):
        validate_settings(data)
