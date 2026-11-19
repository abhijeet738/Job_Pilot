from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from jobpilot.settings import Preferences, Settings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def _real_prefs() -> dict[str, Any]:
    data = yaml.safe_load((CONFIG_DIR / "preferences.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_example_preferences_validate() -> None:
    prefs = Settings(config_dir=CONFIG_DIR).preferences
    assert prefs.target_roles
    assert prefs.caps.per_day <= prefs.caps.hard_max_per_day
    assert 0 <= prefs.matching.fit_threshold <= 100


def test_approval_date_is_parsed_as_date() -> None:
    prefs = Preferences.model_validate(_real_prefs())
    assert isinstance(prefs.autonomy.require_approval_until, date)


def test_unknown_top_level_key_is_rejected() -> None:
    data = _real_prefs()
    data["fit_treshold"] = 70  # typo
    with pytest.raises(ValidationError):
        Preferences.model_validate(data)


def test_missing_required_key_is_rejected() -> None:
    data = _real_prefs()
    del data["target_roles"]
    with pytest.raises(ValidationError):
        Preferences.model_validate(data)


@pytest.mark.parametrize(
    ("section", "key", "bad"),
    [
        ("matching", "fit_threshold", 150),
        ("matching", "fit_threshold", -1),
        ("caps", "per_day", 0),
        ("autonomy", "auto_submit_min_confidence", 1.5),
        ("schedule", "apply_every_minutes", 0),
    ],
)
def test_out_of_range_values_are_rejected(section: str, key: str, bad: float) -> None:
    data = _real_prefs()
    data[section][key] = bad
    with pytest.raises(ValidationError):
        Preferences.model_validate(data)


def test_invalid_approval_date_is_rejected() -> None:
    data = _real_prefs()
    data["autonomy"]["require_approval_until"] = "not-a-date"
    with pytest.raises(ValidationError):
        Preferences.model_validate(data)


def test_load_yaml_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Settings(config_dir=tmp_path).load_yaml("nope.yaml")


def test_load_yaml_rejects_non_mapping(tmp_path: Path) -> None:
    (tmp_path / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        Settings(config_dir=tmp_path).load_yaml("list.yaml")


def test_dry_run_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBPILOT_DRY_RUN", raising=False)
    assert Settings(_env_file=None).jobpilot_dry_run is True  # type: ignore[call-arg]


def test_get_settings_creates_data_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBPILOT_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        s = get_settings()
        for sub in ("profile", "resumes", "artifacts", "browser_profile", "logs"):
            assert (s.jobpilot_data_dir / sub).is_dir()
    finally:
        get_settings.cache_clear()