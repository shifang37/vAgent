"""M8 E1: read-only provider route preview (``provider-route-v1``).

The preview answers one question per published provider: *could this shot or
episode be registered with it right now, and at what frozen price?* It never
registers a Job, never writes storage, never contacts a provider, and only reads
whether a key is configured (never its content).

The output is deterministic: no clock, no randomness, no external I/O. One input
on one baseline produces byte-identical JSON, which is what makes the frozen
``routeProbe`` in ``docs/M8_E0_BASELINE.json`` a reproducible reference.

M8 boundary (contracts §3): this module adds new types only; the nine hashed
contract modules (``vagent.storage``, ``video.contracts``/``films``/``episodes``/
``storyboards``/``renders``/``audio``/``subtitles``/``tts``) stay byte-identical.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, JsonTuple, Name
from vagent.errors import AppError
from vagent.video.contracts import (
    FAL_MAX_JOB_USD,
    FAL_PRICE_VERSION,
    KLING_MAX_JOB_UNITS,
    KLING_MAX_JOB_USD,
    KLING_PRICE_VERSION,
    SHOT_DURATION_MAX,
    SHOT_DURATION_MIN,
    WAN_I2V_MODEL,
    WAN_I2V_PRICE_VERSION,
    WAN_MODEL,
    WAN_PRICE_VERSION,
    Money,
    ProviderModel,
    UnitPrice,
    VideoCapabilities,
    canonical_fingerprint,
    multi_provider_capabilities,
    resource_estimate,
    wan_shot_capabilities,
)

ROUTE_CONTRACT = "provider-route-v1"
CURRENCY_ORDER = ("CNY", "USD")
PROVIDER_ORDER = ("wan", "kling", "fal")
SHOT_MODES = ("t2v", "i2v", "flf2v")
SCOPES = ("shot", "episode", "batch")

# The frozen availability precedence (§3.3): environment -> credentials ->
# capability -> price -> accounting. The first hit is the only reported reason.
REASON_PRECEDENCE = (
    "VIDEO_MODE_OFF",
    "VIDEO_PROVIDER_UNAVAILABLE",
    "VIDEO_KEY_MISSING",
    "VIDEO_UNSUPPORTED_MODE",
    "VIDEO_UNSUPPORTED_SPEC",
    "VIDEO_PRICE_UNKNOWN",
    "EPISODE_COST_LIMIT",
    "VIDEO_COST_LIMIT",
)

# E0 §2.5: capability and price tables rest on documentation only until E3.
# This is a static label in routing.py, deliberately not a capability field, so a
# successful real sample never changes capabilitiesSha256.
VERIFICATION = "documented-no-real-sample"
PROVIDER_VERIFICATION = {"wan": VERIFICATION, "kling": VERIFICATION, "fal": VERIFICATION}

RouteSpec = tuple[str, int, "Literal['16:9'] | None"]


@dataclass(frozen=True)
class JobCeiling:
    """The effective per-Job ceiling the registration path would enforce."""

    amount: str
    currency: str
    units: int | None = None


DEFAULT_JOB_CEILINGS = {
    "wan": JobCeiling(amount="3.00", currency="CNY"),
    "kling": JobCeiling(amount="0.60", currency="USD", units=KLING_MAX_JOB_UNITS),
    "fal": JobCeiling(amount=FAL_MAX_JOB_USD, currency="USD"),
}


def provider_verification(provider: str) -> str:
    """The static real-sample label of one provider (never a capability table field)."""
    return PROVIDER_VERIFICATION.get(provider, VERIFICATION)


def _money(value) -> str:
    return format(Decimal(value), ".2f")


def effective_job_ceilings(config) -> dict[str, JobCeiling]:
    """The per-op ceilings a registration would enforce, in each provider's currency.

    Wan's ceiling is the local ``videoMaxJobCost`` (users may raise it); Kling and
    fal can only tighten their frozen constants, never exceed them (M3 §4).
    """
    return {
        "wan": JobCeiling(amount=_money(config.video_max_job_cost), currency="CNY"),
        "kling": JobCeiling(
            amount=_money(min(Decimal(config.kling_max_job_cost), Decimal(KLING_MAX_JOB_USD))),
            currency="USD",
            units=min(int(config.kling_max_job_units), KLING_MAX_JOB_UNITS),
        ),
        "fal": JobCeiling(
            amount=_money(min(Decimal(config.fal_max_job_cost), Decimal(FAL_MAX_JOB_USD))),
            currency="USD",
        ),
    }


class RouteRequest(Contract):
    """One preview input: either a storyboard reference or one explicit spec.

    ``provider`` optionally names a provider the caller is considering; it is an
    assertion ("is this one published?"), never a silent switch. ``mode`` stays
    null for a storyboard reference, whose shots decide their own specs.
    """

    scope: Literal["shot", "episode", "batch"] = "shot"
    mode: Literal["t2v", "i2v", "flf2v"] | None = None
    duration_seconds: int | None = Field(default=None, ge=SHOT_DURATION_MIN, le=SHOT_DURATION_MAX)
    aspect_ratio: Literal["16:9"] | None = None
    storyboard_id: Identifier | None = None
    storyboard_version: int | None = Field(default=None, ge=1)
    shot_ids: JsonTuple[Identifier] = Field(default=(), max_length=64)
    provider: Literal["wan", "kling", "fal"] | None = None

    @model_validator(mode="after")
    def exactly_one_form(self):
        by_storyboard = self.storyboard_id is not None
        by_spec = self.mode is not None or self.duration_seconds is not None or self.aspect_ratio is not None
        if by_storyboard and by_spec:
            raise ValueError("storyboardId and mode are mutually exclusive")
        if not by_storyboard and self.mode is None:
            raise ValueError("A preview needs storyboardId, or mode and durationSeconds")
        if by_storyboard:
            if self.duration_seconds is not None or self.aspect_ratio is not None:
                raise ValueError("A storyboard preview takes its specs from the shots")
        elif self.duration_seconds is None:
            raise ValueError("An explicit preview needs durationSeconds")
        if self.shot_ids and not by_storyboard:
            raise ValueError("shotIds only apply to a storyboard preview")
        if len(set(self.shot_ids)) != len(self.shot_ids):
            raise ValueError("Shot IDs must be unique inside one preview")
        if self.mode in {"i2v", "flf2v"} and self.aspect_ratio is not None:
            raise ValueError("Image modes inherit the input frame; no explicit aspect ratio")
        if self.mode == "t2v" and self.aspect_ratio != "16:9" and not by_storyboard:
            raise ValueError("Text-to-video previews fix the 16:9 frame")
        return self


class RouteCandidate(Contract):
    """One provider's frozen answer: identity, quote, availability and the reason.

    ``model``/``capabilitiesVersion`` identify the selected published entry (null
    only when the caller named a provider that publishes nothing). The quote block
    is all-or-nothing: it exists only when the request is inside the entry's
    verified specs and its price is the current frozen version.
    """

    provider: Name
    model: ProviderModel | None = None
    capabilities_version: Name | None = None
    price_version: Name | None = None
    currency: Literal["CNY", "USD"] | None = None
    unit: Literal["output_second"] | None = None
    unit_price: UnitPrice | None = None
    quantity: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,8}$")
    amount: Money | None = None
    resource_units: int | None = Field(default=None, ge=0)
    available: bool
    reason: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    key_configured: bool
    verification: Literal["documented-no-real-sample"] = VERIFICATION
    source_url: str | None = Field(default=None, min_length=1, max_length=1000, pattern=r"^https://")
    checked_at: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    quote_fingerprint: Fingerprint | None = None

    @model_validator(mode="after")
    def coherent_quote(self):
        if self.available == (self.reason is not None):
            raise ValueError("An available candidate has no reason; a blocked one has exactly one")
        if self.model is None and self.capabilities_version is not None:
            raise ValueError("A published entry always names its model")
        quote = (
            self.price_version,
            self.currency,
            self.unit,
            self.unit_price,
            self.quantity,
            self.amount,
        )
        if any(value is None for value in quote) and any(value is not None for value in quote):
            raise ValueError("A quote is all-or-nothing")
        if self.amount is None:
            if self.quote_fingerprint is not None or self.resource_units is not None:
                raise ValueError("No quote means no quote fingerprint and no resource units")
        else:
            if Decimal(self.unit_price) * Decimal(self.quantity) != Decimal(self.amount):
                raise ValueError("A quote must equal its frozen unit price times quantity")
            if self.quote_fingerprint != quote_fingerprint(self):
                raise ValueError("A quote always carries the fingerprint of its priced fields")
        if self.available and self.amount is None:
            raise ValueError("An available candidate carries its frozen quote")
        return self


class RoutePreview(Contract):
    """One deterministic comparison of every published provider for one request."""

    contract: Literal["provider-route-v1"] = ROUTE_CONTRACT
    scope: Literal["shot", "episode", "batch"]
    request: RouteRequest
    video_mode: Literal["off", "mock", "live"]
    candidates: JsonTuple[RouteCandidate] = Field(min_length=1, max_length=4)
    recommended: Name | None = None
    notes: JsonTuple[str] = Field(default=(), max_length=8)
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def coherent_preview(self):
        if self.scope != self.request.scope:
            raise ValueError("The preview scope must match its request")
        keys = [(item.provider, item.available) for item in self.candidates]
        if len({item.provider for item in self.candidates}) != len(self.candidates):
            raise ValueError("Each provider appears exactly once")
        available = [provider for provider, ready in keys if ready]
        if self.recommended is not None and self.recommended not in available:
            raise ValueError("Only an available candidate can be recommended")
        if self.recommended is None and available:
            raise ValueError("A comparison with an available candidate must recommend one")
        if self.fingerprint != preview_fingerprint(self.request, self.candidates, self.recommended):
            raise ValueError("The preview fingerprint must match its content")
        return self


def preview_fingerprint(
    request: RouteRequest,
    candidates: Sequence[RouteCandidate],
    recommended: str | None,
) -> str:
    """Cover scope, request, candidate order and the recommendation; never a clock."""
    return canonical_fingerprint(
        {
            "contract": ROUTE_CONTRACT,
            "scope": request.scope,
            "request": request.model_dump(mode="json", by_alias=True),
            "candidates": [
                {
                    "provider": item.provider,
                    "available": item.available,
                    "reason": item.reason,
                    "quoteFingerprint": item.quote_fingerprint,
                }
                for item in candidates
            ],
            "recommended": recommended,
        }
    )


def quote_fingerprint(candidate: RouteCandidate) -> str:
    """The frozen quote identity: never includes time, only the priced fields."""
    return _quote_digest(
        provider=candidate.provider,
        model=candidate.model,
        capabilities_version=candidate.capabilities_version,
        price_version=candidate.price_version,
        currency=candidate.currency,
        unit_price=candidate.unit_price,
        quantity=candidate.quantity,
        amount=candidate.amount,
        resource_units=candidate.resource_units,
    )


def _quote_digest(
    *,
    provider: str,
    model: str | None,
    capabilities_version: str | None,
    price_version: str | None,
    currency: str | None,
    unit_price: str | None,
    quantity: str | None,
    amount: str | None,
    resource_units: int | None,
) -> str:
    return canonical_fingerprint(
        {
            "provider": provider,
            "model": model,
            "capabilitiesVersion": capabilities_version,
            "priceVersion": price_version,
            "currency": currency,
            "unitPrice": unit_price,
            "quantity": quantity,
            "amount": amount,
            "resourceUnits": resource_units,
        }
    )


def make_request(
    *,
    scope: str = "shot",
    mode: str | None = None,
    duration_seconds: int | None = None,
    aspect_ratio: str | None = None,
    storyboard_id: str | None = None,
    storyboard_version: int | None = None,
    shot_ids: Sequence[str] = (),
    provider: str | None = None,
) -> RouteRequest:
    """Normalize one caller input, mapping contract violations to the frozen codes.

    A storyboard and an explicit spec are mutually exclusive (§3.1). Values that
    make the request unanswerable (unknown scope/mode/provider, an out-of-range
    duration) are ``ROUTE_UNAVAILABLE``; a malformed argument set is
    ``INVALID_ARGUMENTS``. Nothing is registered either way.
    """
    if scope not in SCOPES:
        raise AppError("ROUTE_UNAVAILABLE", "scope 只能是 shot、episode 或 batch。")
    if provider is not None and provider not in PROVIDER_ORDER:
        raise AppError("ROUTE_UNAVAILABLE", "只支持 wan、kling 或 fal 三个已发布供应商。")
    if mode is not None and mode not in SHOT_MODES:
        raise AppError("ROUTE_UNAVAILABLE", "mode 只能是 t2v、i2v 或 flf2v。")
    by_storyboard = storyboard_id is not None
    by_spec = mode is not None or duration_seconds is not None or aspect_ratio is not None
    if by_storyboard and by_spec:
        raise AppError("INVALID_ARGUMENTS", "storyboardId 与 mode 只能二选一，未做任何登记。")
    if not by_storyboard and mode is None:
        raise AppError("INVALID_ARGUMENTS", "需要 --storyboard，或 --mode 与 --duration。")
    if shot_ids and not by_storyboard:
        raise AppError("INVALID_ARGUMENTS", "shotIds 只能与 storyboardId 一起使用。")
    if storyboard_version is not None and storyboard_version < 1:
        raise AppError("ROUTE_UNAVAILABLE", "分镜版本必须是正整数。")
    if by_spec:
        if duration_seconds is None:
            raise AppError("INVALID_ARGUMENTS", "显式预览需要 durationSeconds。")
        if not isinstance(duration_seconds, int) or isinstance(duration_seconds, bool):
            raise AppError("ROUTE_UNAVAILABLE", "durationSeconds 必须是整数。")
        if not SHOT_DURATION_MIN <= duration_seconds <= SHOT_DURATION_MAX:
            raise AppError(
                "ROUTE_UNAVAILABLE",
                f"durationSeconds 必须在 {SHOT_DURATION_MIN}–{SHOT_DURATION_MAX} 秒之间。",
            )
        if mode == "t2v":
            aspect_ratio = aspect_ratio or "16:9"
            if aspect_ratio != "16:9":
                raise AppError("ROUTE_UNAVAILABLE", "当前只核实了 16:9 画幅。")
        elif aspect_ratio is not None:
            raise AppError("ROUTE_UNAVAILABLE", "图生视频的画幅由输入帧决定，不能显式指定。")
    try:
        return RouteRequest(
            scope=scope,
            mode=mode,
            duration_seconds=duration_seconds,
            aspect_ratio=aspect_ratio,
            storyboard_id=storyboard_id,
            storyboard_version=storyboard_version,
            shot_ids=tuple(shot_ids),
            provider=provider,
        )
    except (ValidationError, ValueError):
        raise AppError("INVALID_ARGUMENTS", "预览参数不符合 provider-route-v1 契约。") from None


def resolve(request: RouteRequest, state: Mapping) -> tuple[RouteRequest, tuple[RouteSpec, ...]]:
    """Resolve a storyboard reference to its per-shot specs, read-only.

    Unknown storyboards, versions and shot IDs make the request unanswerable, so
    they are ``ROUTE_UNAVAILABLE`` rather than a partial answer. Shot order is the
    caller's order (or the saved storyboard order), so the first failing shot is
    reproducible.
    """
    if request.storyboard_id is None:
        return request, ((request.mode, request.duration_seconds, request.aspect_ratio),)
    from vagent.video.storyboards import Storyboard

    raw = (state.get("storyboards") or {}).get(request.storyboard_id)
    if raw is None:
        raise AppError("ROUTE_UNAVAILABLE", "当前数据目录中没有这个分镜，未做任何登记。")
    try:
        record = Storyboard.model_validate(raw)
    except (ValidationError, ValueError):
        raise AppError("ROUTE_UNAVAILABLE", "分镜记录无法读取，未做任何登记。") from None
    version = request.storyboard_version or record.current_version
    frozen = next((item for item in record.versions if item.version == version), None)
    if frozen is None:
        raise AppError("ROUTE_UNAVAILABLE", "这个分镜版本不存在，未做任何登记。")
    shots = {shot.id: shot for shot in frozen.shots}
    chosen = tuple(request.shot_ids) or tuple(shot.id for shot in frozen.shots)
    if any(shot_id not in shots for shot_id in chosen):
        raise AppError("ROUTE_UNAVAILABLE", "有镜头不属于这个分镜版本，未做任何登记。")
    specs = tuple(
        (
            shots[shot_id].mode,
            shots[shot_id].duration_seconds,
            "16:9" if shots[shot_id].mode == "t2v" else None,
        )
        for shot_id in chosen
    )
    return request.model_copy(update={"storyboard_version": version, "shot_ids": chosen}), specs


# --- frozen entry selection and quoting ---------------------------------------


def _entries(provider: str, capabilities: Sequence) -> tuple:
    return tuple(
        entry
        for entry in capabilities
        if entry.provider == provider and getattr(entry, "mode", "mock") == "live"
    )


def _spec_matches(entry, spec: RouteSpec) -> bool:
    mode, duration, aspect = spec
    return any(
        item.mode == mode
        and int(item.duration_seconds) == duration
        and (mode != "t2v" or item.aspect_ratio == aspect)
        for item in entry.specs
    )


def _select_entry(entries: tuple, spec: RouteSpec):
    """One entry per provider: a matching spec, else the mode, else its first entry."""
    mode = spec[0]
    for entry in entries:
        if mode in tuple(getattr(entry, "shot_modes", ("t2v",))) and _spec_matches(entry, spec):
            return entry
    for entry in entries:
        if mode in tuple(getattr(entry, "shot_modes", ("t2v",))):
            return entry
    return entries[0]


def _price_of(entry):
    price = getattr(entry, "price", None)
    return price if price is not None else getattr(entry, "rate", None)


def _expected_price_version(entry) -> str | None:
    if entry.provider == "wan":
        return {
            WAN_MODEL: WAN_PRICE_VERSION,
            WAN_I2V_MODEL: WAN_I2V_PRICE_VERSION,
        }.get(entry.model)
    return {"kling": KLING_PRICE_VERSION, "fal": FAL_PRICE_VERSION}.get(entry.provider)


@dataclass(frozen=True)
class _Quote:
    model: str
    capabilities_version: str
    price_version: str
    currency: str
    unit: str
    unit_price: str
    quantity: str
    amount: str
    resource_units: int | None
    source_url: str
    checked_at: str
    mixed: bool

    def candidate_fields(self) -> dict:
        return {
            "model": self.model,
            "capabilities_version": self.capabilities_version,
            "price_version": self.price_version,
            "currency": self.currency,
            "unit": self.unit,
            "unit_price": self.unit_price,
            "quantity": self.quantity,
            "amount": self.amount,
            "resource_units": self.resource_units,
            "source_url": self.source_url,
            "checked_at": self.checked_at,
        }


def _quote(priced: Sequence[tuple[object, RouteSpec, object]]) -> _Quote | None:
    """Sum one provider's per-shot quotes; None when they cannot form one quote.

    Every published entry bills a flat per-output-second price, so the combined
    amount is still exactly ``unitPrice x total seconds``; a table whose entries
    disagree on the rate or currency cannot produce one frozen quote.
    """
    first_entry, _, first_price = priced[0]
    currency, unit, unit_price = first_price.currency, first_price.unit, first_price.unit_price
    quantity = 0
    units: int | None = None
    mixed = False
    for entry, spec, price in priced:
        if (price.currency, price.unit, price.unit_price) != (currency, unit, unit_price):
            return None
        if (price.price_version, price.source_url, price.checked_at) != (
            first_price.price_version,
            first_price.source_url,
            first_price.checked_at,
        ):
            mixed = True
        quantity += spec[1]
        entry_units = (
            resource_estimate(entry.resource, spec[1]).units
            if getattr(entry, "resource", None) is not None
            else None
        )
        if entry_units is not None:
            units = (units or 0) + entry_units
        elif units is not None:
            return None
    return _Quote(
        model=first_entry.model,
        capabilities_version=first_entry.capabilities_version,
        price_version=first_price.price_version,
        currency=currency,
        unit=unit,
        unit_price=unit_price,
        quantity=str(quantity),
        amount=_money(Decimal(unit_price) * quantity),
        resource_units=units,
        source_url=first_price.source_url,
        checked_at=first_price.checked_at,
        mixed=mixed,
    )


def _exceeds(quote: _Quote, priced: Sequence[tuple[object, RouteSpec, object]], ceiling: JobCeiling) -> bool:
    """Registration checks every Job against the per-Job amount and unit ceiling."""
    if quote.currency != ceiling.currency:
        return True
    for entry, spec, _ in priced:
        if Decimal(quote.unit_price) * spec[1] > Decimal(ceiling.amount):
            return True
        if ceiling.units is not None and getattr(entry, "resource", None) is not None:
            if resource_estimate(entry.resource, spec[1]).units > ceiling.units:
                return True
    return False


def _candidate(
    provider: str,
    entries: tuple,
    specs: tuple[RouteSpec, ...],
    *,
    key_configured: bool,
    video_mode: str,
    scope: str,
    ceiling: JobCeiling | None,
) -> tuple[RouteCandidate, bool]:
    selected = tuple(_select_entry(entries, spec) for spec in specs)
    reason = None
    for entry, spec in zip(selected, specs, strict=True):
        if spec[0] not in tuple(getattr(entry, "shot_modes", ("t2v",))):
            reason = "VIDEO_UNSUPPORTED_MODE"
            break
    if reason is None:
        for entry, spec in zip(selected, specs, strict=True):
            if not _spec_matches(entry, spec):
                reason = "VIDEO_UNSUPPORTED_SPEC"
                break
    quote, priced = None, None
    if reason is None:
        priced = []
        for entry, spec in zip(selected, specs, strict=True):
            price = _price_of(entry)
            if price is None or price.price_version != _expected_price_version(entry):
                reason = "VIDEO_PRICE_UNKNOWN"
                break
            priced.append((entry, spec, price))
        if reason is None:
            quote = _quote(priced)
            if quote is None:
                reason = "VIDEO_PRICE_UNKNOWN"
    if video_mode in {"off", "mock"}:
        reason = "VIDEO_MODE_OFF"
    elif not key_configured:
        reason = "VIDEO_KEY_MISSING"
    elif reason is None and scope in {"episode", "batch"} and quote.currency != "CNY":
        reason = "EPISODE_COST_LIMIT"
    elif reason is None and ceiling is not None and _exceeds(quote, priced, ceiling):
        reason = "VIDEO_COST_LIMIT"
    fields = {
        "provider": provider,
        "model": selected[0].model,
        "capabilities_version": selected[0].capabilities_version,
        "available": reason is None,
        "reason": reason,
        "key_configured": key_configured,
    }
    if quote is not None:
        fields.update(quote.candidate_fields())
        fields["quote_fingerprint"] = _quote_digest(
            provider=provider,
            model=quote.model,
            capabilities_version=quote.capabilities_version,
            price_version=quote.price_version,
            currency=quote.currency,
            unit_price=quote.unit_price,
            quantity=quote.quantity,
            amount=quote.amount,
            resource_units=quote.resource_units,
        )
    return RouteCandidate(**fields), bool(quote is not None and quote.mixed)


def _placeholder(provider: str, *, key_configured: bool) -> RouteCandidate:
    return RouteCandidate(
        provider=provider,
        available=False,
        reason="VIDEO_PROVIDER_UNAVAILABLE",
        key_configured=key_configured,
    )


def preview(
    request: RouteRequest,
    *,
    specs: Sequence[RouteSpec] | None = None,
    capabilities: Sequence[VideoCapabilities] | None = None,
    keys_configured: Mapping[str, bool] | None = None,
    video_mode: str = "live",
    job_ceilings: Mapping[str, JobCeiling] | None = None,
    require_available: bool = False,
) -> RoutePreview:
    """Compare every published provider for one resolved request (read-only).

    ``specs`` defaults to the request's own mode/duration; a storyboard request is
    resolved by :func:`resolve` first. ``keys_configured`` defaults to "not
    configured" so an omitted argument never implies a usable credential.
    """
    if video_mode not in {"off", "mock", "live"}:
        raise AppError("INVALID_ARGUMENTS", "videoMode 只能是 off、mock 或 live。")
    if specs is None:
        if request.mode is None:
            raise AppError("INVALID_ARGUMENTS", "按分镜预览需要先解析镜头规格。")
        specs = ((request.mode, request.duration_seconds, request.aspect_ratio),)
    specs = tuple(specs)
    if not specs:
        raise AppError("ROUTE_UNAVAILABLE", "没有可预览的镜头。")
    for mode, duration, aspect in specs:
        if mode not in SHOT_MODES:
            raise AppError("ROUTE_UNAVAILABLE", "mode 只能是 t2v、i2v 或 flf2v。")
        if not isinstance(duration, int) or not SHOT_DURATION_MIN <= duration <= SHOT_DURATION_MAX:
            raise AppError("ROUTE_UNAVAILABLE", "镜头时长不在已核实的 2–15 秒范围内。")
        if mode == "t2v" and aspect != "16:9":
            raise AppError("ROUTE_UNAVAILABLE", "文生视频只核实了 16:9 画幅。")
        if mode != "t2v" and aspect is not None:
            raise AppError("ROUTE_UNAVAILABLE", "图生视频的画幅由输入帧决定。")
    table = (
        tuple(capabilities)
        if capabilities is not None
        else (
            *wan_shot_capabilities(),
            *multi_provider_capabilities(),
        )
    )
    keys = {provider: bool((keys_configured or {}).get(provider, False)) for provider in PROVIDER_ORDER}
    ceilings = dict(job_ceilings or DEFAULT_JOB_CEILINGS)

    candidates, notes = [], []
    mixed = False
    for provider in PROVIDER_ORDER:
        entries = _entries(provider, table)
        if not entries:
            if request.provider == provider:
                candidates.append(_placeholder(provider, key_configured=keys[provider]))
            continue
        candidate, provider_mixed = _candidate(
            provider,
            entries,
            specs,
            key_configured=keys[provider],
            video_mode=video_mode,
            scope=request.scope,
            ceiling=ceilings.get(provider, DEFAULT_JOB_CEILINGS[provider]),
        )
        candidates.append(candidate)
        mixed = mixed or provider_mixed
    if not candidates:
        raise AppError("ROUTE_UNAVAILABLE", "当前没有已发布的可比价供应商。")

    available = [item for item in candidates if item.available]
    blocked = [item for item in candidates if not item.available]
    available.sort(
        key=lambda item: (
            CURRENCY_ORDER.index(item.currency),
            Decimal(item.amount),
            PROVIDER_ORDER.index(item.provider),
        )
    )
    blocked.sort(key=lambda item: PROVIDER_ORDER.index(item.provider))
    ordered = (*available, *blocked)
    recommended = available[0].provider if available else None

    notes.append("不换汇：CNY 与 USD 分别计账、分别展示，不做汇率比较或跨币种合计。")
    notes.append(
        "集级/批量只接受 CNY，USD 供应商不可用，也不计入额度。"
        if request.scope in {"episode", "batch"}
        else "USD 不计入集级/批量额度。"
    )
    if video_mode != "live":
        notes.append(f"当前视频模式为 {video_mode}：预览仍按 live 能力表与冻结价格展示，但不能登记真实任务。")
    notes.append("预览只核对冻结能力、价格与单 Job 上限；不预测 Run 剩余额度、Job 名额与限流冷却。")
    if mixed:
        notes.append("所选镜头跨多个已核实条目：金额为逐镜头报价之和，行内 model/priceVersion 取第一个镜头。")
    if any(item.reason == "VIDEO_PROVIDER_UNAVAILABLE" for item in ordered):
        notes.append("显式指定的供应商没有已发布条目，因此不可用。")

    result = RoutePreview(
        scope=request.scope,
        request=request,
        video_mode=video_mode,
        candidates=ordered,
        recommended=recommended,
        notes=tuple(notes),
        fingerprint=preview_fingerprint(request, ordered, recommended),
    )
    if require_available and not available:
        raise AppError("ROUTE_UNAVAILABLE", "没有任何供应商可以登记这个请求，未做任何登记。")
    return result


def preview_for(
    config,
    state: Mapping,
    request: RouteRequest,
    *,
    capabilities: Sequence[VideoCapabilities] | None = None,
    require_available: bool = False,
) -> RoutePreview:
    """Resolve one request against local state and the caller's saved settings.

    Only key *presence*, the video mode and the effective ceilings are read from
    the configuration; no credential content leaves ``Config`` and no provider
    object is built.
    """
    resolved, specs = resolve(request, state)
    return preview(
        resolved,
        specs=specs,
        capabilities=capabilities,
        keys_configured={
            "wan": bool(config.video_api_key),
            "kling": bool(getattr(config, "kling_api_key", None)),
            "fal": bool(getattr(config, "fal_api_key", None)),
        },
        video_mode=getattr(config, "video_mode", "live"),
        job_ceilings=effective_job_ceilings(config),
        require_available=require_available,
    )


__all__ = [
    "CURRENCY_ORDER",
    "DEFAULT_JOB_CEILINGS",
    "JobCeiling",
    "PROVIDER_ORDER",
    "PROVIDER_VERIFICATION",
    "REASON_PRECEDENCE",
    "ROUTE_CONTRACT",
    "RouteCandidate",
    "RoutePreview",
    "RouteRequest",
    "VERIFICATION",
    "effective_job_ceilings",
    "make_request",
    "preview",
    "preview_fingerprint",
    "preview_for",
    "provider_verification",
    "quote_fingerprint",
    "resolve",
]
