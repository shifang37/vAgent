"""Strict JSON contracts for new execution features; independent of storage/SDKs."""

from datetime import datetime, timedelta
from typing import Annotated, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
    )


def _identifier(value: str) -> str:
    if value in {"__proto__", "constructor", "prototype"}:
        raise ValueError("Reserved identifier")
    return value


Identifier = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"),
    AfterValidator(_identifier),
]
Name = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$")]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


def _utc_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Expected an ISO 8601 UTC timestamp") from None
    if "T" not in value or parsed.utcoffset() != timedelta(0):
        raise ValueError("Expected an ISO 8601 UTC timestamp")
    return value


UtcTimestamp = Annotated[str, Field(min_length=20, max_length=40), AfterValidator(_utc_timestamp)]


def _json_array(value):
    # JSON arrays become immutable tuples; do not coerce arbitrary iterables.
    if type(value) is list:
        return tuple(value)
    return value


T = TypeVar("T")
JsonTuple = Annotated[tuple[T, ...], BeforeValidator(_json_array)]
