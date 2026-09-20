import pytest

from app.models import InvalidStateTransition, JobStatus, validate_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobStatus.PENDING, JobStatus.PROCESSING),
        (JobStatus.PROCESSING, JobStatus.SENT),
        (JobStatus.PROCESSING, JobStatus.RETRY),
        (JobStatus.RETRY, JobStatus.PROCESSING),
        (JobStatus.PROCESSING, JobStatus.WAITING_RATE_LIMIT),
        (JobStatus.WAITING_RATE_LIMIT, JobStatus.PROCESSING),
        (JobStatus.PROCESSING, JobStatus.FAILED),
        (JobStatus.SCHEDULED, JobStatus.PENDING),
        (JobStatus.PENDING, JobStatus.CANCELLED),
    ],
)
def test_expected_transitions_are_legal(current: JobStatus, target: JobStatus) -> None:
    validate_transition(current, target)


def test_illegal_transition_is_rejected() -> None:
    with pytest.raises(InvalidStateTransition, match="Illegal job transition"):
        validate_transition(JobStatus.SENT, JobStatus.PROCESSING)
