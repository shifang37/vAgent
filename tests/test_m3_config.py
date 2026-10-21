"""M3 E2: per-provider Keys, hot reload and the unverified new-provider status."""

import json

import pytest

from vagent.application import ApplicationService
from vagent.config import ConfigUpdate, config_sources, load_config
from vagent.errors import AppError


async def test_a_kling_key_enables_live_without_any_probe_or_job(tmp_path):
    config = load_config({"VAGENT_HOME": str(tmp_path)})
    async with ApplicationService.open(config) as service:
        before = {item["provider"]: item for item in service.configuration()["providers"]}
        assert set(before) == {"wan", "kling", "fal"}
        assert not any(item["keyConfigured"] for item in before.values())
        result = await service.save_configuration(
            ConfigUpdate(klingApiKey="kling-secret", videoProvider="kling")
        )
        assert result["autoEnabledLive"] is True
        assert result["activeVideoMode"] == result["videoMode"] == "live"
        assert result["klingApiKeyConfigured"] is True and result["videoApiKeyConfigured"] is False
        assert "kling-secret" not in json.dumps(result)
        kling = next(item for item in result["providers"] if item["provider"] == "kling")
        assert kling["keyConfigured"] is True
        assert kling["permissionStatus"] == "unverified"
        assert result["videoProbe"] is None  # Only the Wan Key has a zero-cost probe.
        assert service.kling_adapter._key == "kling-secret"
        assert service.video_jobs.config.video_provider == "kling"
        assert service.kling_adapter._key == service.config.kling_api_key
        assert not service.store.snapshot()["jobs"]
        assert not service.store.snapshot()["media"]
        saved = (tmp_path / "config.yml").read_text(encoding="utf-8")
        assert "kling-secret" in saved


async def test_bad_provider_keys_are_rejected_without_echo(tmp_path):
    from pydantic import ValidationError

    config = load_config({"VAGENT_HOME": str(tmp_path)})
    async with ApplicationService.open(config) as service:
        with pytest.raises((AppError, ValidationError)):
            await service.save_configuration(ConfigUpdate(falApiKey="secret\nheader"))
        assert not (tmp_path / "config.yml").exists()


def test_provider_keys_use_their_frozen_environment_names(tmp_path):
    config = load_config(
        {
            "VAGENT_HOME": str(tmp_path),
            "VAGENT_KLING_API_KEY": "kling-env-key",
            "FAL_KEY": "fal-env-key",
            "VAGENT_VIDEO_PROVIDER": "fal",
        }
    )
    assert config.kling_api_key == "kling-env-key"
    assert config.fal_api_key == "fal-env-key"
    assert config.video_provider == "fal"
    sources = config_sources(config)
    assert sources["klingApiKey"] == "environment" and sources["falApiKey"] == "environment"


def test_saved_provider_ceilings_cannot_exceed_the_frozen_service_caps(tmp_path):
    config = load_config(
        {
            "VAGENT_HOME": str(tmp_path),
            "VAGENT_KLING_MAX_JOB_COST": "0.50",
            "VAGENT_KLING_MAX_RUN_UNITS": "8",
            "VAGENT_FAL_MAX_RUN_COST": "1.50",
        }
    )
    assert config.kling_max_job_cost == "0.50"
    assert config.kling_max_run_units == 8
    assert config.fal_max_run_cost == "1.50"
    with pytest.raises(AppError):
        load_config({"VAGENT_HOME": str(tmp_path), "VAGENT_KLING_MAX_JOB_UNITS": "99"})
