"""What a completed generation is charged against its agent's daily budget.

The budget window used to be reconciled to the gateway's estimate every time,
because no adapter reported usage. OpenAI and Gemini now report what they
billed, so the attempt that answered is charged that; attempts that failed
before it report nothing and are still charged their input estimate. The run's
evidence names which (`basis`), and keeps the estimate beside it.
"""

from dataclasses import replace

from aida.agent_orchestrator import run_token_charge
from aida.model_gateway import ModelCallEvidence


def _evidence(*, billed: tuple[int, int] | None) -> ModelCallEvidence:
    return ModelCallEvidence(
        route="primary",
        provider_type="OPENAI",
        model_id="gpt-4o-mini",
        endpoint_alias="openai-public-api",
        input_fingerprint="0" * 64,
        output_fingerprint="1" * 64,
        input_size_bytes=4_000,
        output_size_bytes=400,
        schema_name="SqlGenerationOutput",
        estimated_input_tokens=1_000,
        estimated_output_tokens=100,
        provider_input_tokens=None if billed is None else billed[0],
        provider_output_tokens=None if billed is None else billed[1],
    )


def test_a_provider_that_reports_nothing_is_charged_the_estimate() -> None:
    charge = run_token_charge(_evidence(billed=None), attempt_count=1)

    assert (charge.charged, charge.estimated, charge.billed) == (1_100, 1_100, None)
    assert charge.basis == "ESTIMATED_NOT_PROVIDER_REPORTED"


def test_a_reported_answer_is_charged_what_the_provider_billed() -> None:
    charge = run_token_charge(_evidence(billed=(850, 60)), attempt_count=1)

    assert (charge.charged, charge.estimated, charge.billed) == (910, 1_100, 910)
    assert charge.basis == "PROVIDER_REPORTED"


def test_attempts_that_failed_first_still_cost_their_input_estimate() -> None:
    """A fallback answered after the primary's 503. The primary sent the same
    payload and reported nothing, so it is charged its input estimate."""
    charge = run_token_charge(_evidence(billed=(850, 60)), attempt_count=2)

    assert charge.charged == 910 + 1_000
    assert charge.estimated == 1_000 * 2 + 100
    assert charge.basis == "PROVIDER_REPORTED_PLUS_ESTIMATED_FAILED_ATTEMPTS"


def test_half_a_report_is_no_report() -> None:
    partial = replace(_evidence(billed=(850, 60)), provider_output_tokens=None)

    charge = run_token_charge(partial, attempt_count=1)

    assert charge.basis == "ESTIMATED_NOT_PROVIDER_REPORTED"
    assert charge.charged == charge.estimated == 1_100
