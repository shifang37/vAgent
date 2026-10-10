import hashlib
import json

import pytest
from pydantic import TypeAdapter, ValidationError
from wan_support import VIDEO_URL, frozen_request, live_arguments, live_run, live_service, provider_response

from vagent.errors import AppError
from vagent.video.contracts import (
    ActualCost,
    JobResultV2,
    MediaAsset,
    MediaMetadata,
    Money,
    PreparedMedia,
    VideoIntentV2,
    WanParameters,
    parse_job,
)
from vagent.video.jobs import changed_job
from vagent.video.worker import JobWorker


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def test_versioned_fingerprints_cover_full_request_and_keep_intent_independent_of_scope():
    request = frozen_request(prompt="  原提示\n 内部空白。 ")
    public = live_arguments(prompt="原提示\n 内部空白。", sourceRefs=[])
    assert request.intent_fingerprint() == digest({"intentVersion": 2, "arguments": public})
    complete = {
        **public,
        "region": "cn-beijing",
        "workspaceId": "test-workspace",
        "adapterVersion": "wan-http-v1",
        "endpointProfile": "beijing-workspace-v1",
        "providerPrompt": "生成单镜头视频。\n原提示\n 内部空白。",
        "parameters": {
            "resolution": "720P",
            "ratio": "16:9",
            "duration": 5,
            "prompt_extend": False,
            "watermark": True,
            "seed": 0,
        },
    }
    assert request.fingerprint() == digest({"requestVersion": 2, "request": complete})
    other_scope = frozen_request(workspace_id="other-space", prompt="原提示\n 内部空白。")
    assert request.intent_fingerprint() == other_scope.intent_fingerprint()
    assert request.fingerprint() != other_scope.fingerprint()
    assert VideoIntentV2.model_validate(public).intent_fingerprint() == request.intent_fingerprint()
    assert frozen_request(prompt="原提示\n内部空白。").intent_fingerprint() != request.intent_fingerprint()


@pytest.mark.parametrize("value", [3, 3.0, True, "NaN", "Infinity", "-1", "3e0", "0.001", "01", "+1"])
def test_money_is_a_finite_nonnegative_decimal_string(value):
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python(value)


def test_money_and_actual_cost_never_infer_a_bill_from_an_estimate():
    assert TypeAdapter(Money).validate_python("3") == "3.00"
    assert TypeAdapter(Money).validate_python("0.6") == "0.60"
    assert ActualCost().model_dump(mode="json") == {
        "status": "unknown",
        "currency": "CNY",
        "amount": None,
        "source": None,
    }
    for value in ({"status": "reported", "amount": "3.00"}, {"amount": "3.00"}, {"source": "usage"}):
        with pytest.raises(ValidationError):
            ActualCost.model_validate(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"duration": True},
        {"duration": "5"},
        {"seed": False},
        {"prompt_extend": 0},
        {"prompt_extend": True},
        {"watermark": False},
        {"size": "1280x720"},
        {"audio_url": VIDEO_URL},
    ],
)
def test_provider_parameters_are_exact_and_strict(changes):
    with pytest.raises(ValidationError):
        WanParameters.model_validate(changes)


@pytest.mark.parametrize("version", [True, "2", 2.0, 0, 3, None])
def test_job_dispatch_only_accepts_exact_version_integers(version):
    with pytest.raises(ValueError):
        parse_job({"contractVersion": version})


async def test_media_index_and_result_must_commit_together_and_remain_immutable(store, video_clock):
    import httpx

    context = live_run(store)
    async with live_service(
        store,
        video_clock,
        lambda _: httpx.Response(200, json=provider_response("SUCCEEDED", video_url=VIDEO_URL)),
    ) as (service, _):
        result = service.generate(live_arguments(), context=context)
        assert result["ok"]
        job = await JobWorker(service).run_once()
        # Domain commit test only: these synthetic measurements are not a claim
        # of MP4 validation, which belongs to C2's media/file worker.
        metadata = MediaMetadata(
            width=1280, height=720, duration_seconds=5, video_codec="h264", has_audio=False
        )
        prepared = PreparedMedia(
            size_bytes=512, sha256="a" * 64, metadata=metadata, validated_at=service.timestamp()
        )
        download = {
            **job.download.model_dump(mode="json", by_alias=True),
            "phase": "committed",
            "attempts": 1,
            "windowAttempts": 1,
            "nextAttemptAt": None,
            "prepared": prepared.model_dump(mode="json", by_alias=True),
        }
        delivered = JobResultV2(
            request_fingerprint=job.request_fingerprint,
            spec=job.request.spec,
            source_refs=job.request.source_refs,
            media_refs=[{"mediaId": job.download.media_id}],
            summary="合成媒体索引测试",
        )
        completed = changed_job(
            job,
            service.timestamp(),
            status="succeeded",
            download=download,
            result=delivered.model_dump(mode="json", by_alias=True),
            mediaAvailability={"status": "available", "reason": None, "checkedAt": service.timestamp()},
        )
        with pytest.raises(AppError) as caught:
            service.save(completed, expected_revision=job.revision)
        assert caught.value.code == "MEDIA_NOT_COMMITTED" and service.get(job.id) == job
        asset = MediaAsset(
            id=job.download.media_id,
            project_id="coffee",
            job_id=job.id,
            relative_path=job.download.relative_path,
            source_refs=job.request.source_refs,
            size_bytes=512,
            sha256="a" * 64,
            created_at=service.timestamp(),
            metadata=metadata,
        )

        def commit(draft):
            draft["media"][asset.id] = asset.model_dump(mode="json", by_alias=True)
            draft["jobs"][job.id] = completed.model_dump(mode="json", by_alias=True)

        store.transaction(commit)
        assert service.get(job.id).result == delivered
        before = store.snapshot()
        with pytest.raises(AppError):
            store.transaction(lambda draft: draft["media"][asset.id].update(sha256="b" * 64))
        assert store.snapshot() == before
        with pytest.raises(AppError) as caught:
            service.update(
                completed,
                result={**delivered.model_dump(mode="json", by_alias=True), "summary": "replacement"},
            )
        assert caught.value.code == "JOB_IMMUTABLE"
