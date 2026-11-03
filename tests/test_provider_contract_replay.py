"""The zero-cost replay of the vendors' documented payloads must keep passing.

``scripts/verify_provider_contract.py`` is the only check that compares the production
Kling/fal adapters against the vendor references rather than against the project's own
fixtures, so it is easy for it to rot silently. Running it from the suite keeps the
documented field names, the status mapping and the service-code table honest.
"""

from scripts import verify_provider_contract as verifier


async def test_the_documented_payloads_still_parse_into_the_frozen_contracts():
    report = await verifier.run()
    assert report["failedChecks"] == []
    assert report["status"] == "passed"
    assert report["networkCalls"] == 0


async def test_every_official_service_code_is_covered_by_a_check():
    report = await verifier.run()
    checked = {item["check"] for item in report["checks"]}
    for code in verifier.OFFICIAL_SERVICE_CODES:
        assert f"kling.service_code.{code}" in checked
