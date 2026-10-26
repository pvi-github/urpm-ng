"""What rpm was asked to do has to reach the parent before it lets go.

A transaction runs in a forked child.  Under smart sync the parent
releases as soon as the extractions are over, by design: the child
goes on running scriptlets and triggers while the operator gets their
prompt back.  The parent therefore **never reads** the ``op_done``
message that ends the operation, and the code says so where it breaks
out of the loop.

So the list of what rpm attempted has to arrive earlier, on the
progress message that marks the end of the extractions, which is what
``_carry_attempts`` is called on.  It was not on that message, and the
message itself was only sent when there was a README to carry:

    if readme_data_early and not pipe_state['closed']:

With no README, nothing was sent, the parent released knowing nothing,
``record_action_outcomes`` returned early, and every package row of the
history stayed at ``planned``.  Measured on a live machine: 24 rows out
of 1912 had ever reached ``done``, and since the post-operation rules
match on ``done``, none of them could ever fire -- not the urpmi
configuration sync, not the declared restarts, none.
"""

from __future__ import annotations

import pytest

from urpm.core.transaction_queue import (
    OperationResult,
    OperationType,
    QueueProgressMessage,
    TransactionQueue,
    _carry_attempts,
)


class _Op:
    """The minimum of a queued operation this message needs."""

    def __init__(self, operation_id="op-1"):
        self.operation_id = operation_id
        self.op_type = OperationType.INSTALL
        self.rpm_paths = []


@pytest.fixture
def queue(monkeypatch):
    """A queue that has just extracted two packages and erased one."""
    queue = TransactionQueue()
    queue._name_by_path = {"/var/cache/a.rpm": "apache",
                           "/var/cache/b.rpm": "bash"}
    queue._erased_names = ["obexd"]
    queue._callback_reasons = {"apache": "never-started"}
    monkeypatch.setattr(queue, "_collect_readme_messages", lambda op: [])
    return queue


class TestTheMessageThatMarksTheEndOfTheExtractions:
    def test_it_carries_what_rpm_was_asked_to_install(self, queue):
        msg = queue._install_done_message(_Op(), 3, 3)

        assert msg.attempted == ["apache", "bash"]

    def test_it_carries_what_was_asked_to_be_erased(self, queue):
        msg = queue._install_done_message(_Op(), 3, 3)

        assert msg.attempted_erases == ["obexd"]

    def test_it_carries_the_reasons_known_so_far(self, queue):
        # Partial by nature: the reasons accumulate as the run goes on.
        # A missing one costs a ``skipped`` refined into ``failed``,
        # never a wrong verdict, the verdict coming from the rpmdb.
        msg = queue._install_done_message(_Op(), 3, 3)

        assert msg.callback_reasons == {"apache": "never-started"}

    def test_it_exists_without_a_readme_to_carry(self, queue):
        # The defect: its only reason to be sent used to be the
        # READMEs, and a transaction without one told the parent
        # nothing at all.
        msg = queue._install_done_message(_Op(), 3, 3)

        assert msg.readme_messages == []
        assert msg.attempted

    def test_it_says_where_the_transaction_really_is(self, queue):
        # Extractions are done, erases may not be: the counters report
        # the elements, not a 100 % the transaction has not reached.
        msg = queue._install_done_message(_Op(), 2, 5)

        assert (msg.current, msg.total) == (2, 5)

    def test_its_phase_is_the_one_the_parent_watches(self, queue):
        assert queue._install_done_message(_Op(), 3, 3).phase == "install_done"

    def test_a_path_that_ran_nothing_carries_nothing(self, queue):
        # What tells the parent to leave the history alone, which is
        # how a ``--test`` must look from here.
        queue._name_by_path = {}
        queue._erased_names = []

        msg = queue._install_done_message(_Op(), 0, 0)

        assert not msg.attempted
        assert not msg.attempted_erases


class TestItSurvivesThePipe:
    """The message is JSON on a pipe between two processes."""

    def test_the_attempts_come_back_out(self, queue):
        sent = queue._install_done_message(_Op(), 3, 3)

        received = QueueProgressMessage.from_json(sent.to_json())

        assert received.attempted == ["apache", "bash"]
        assert received.attempted_erases == ["obexd"]
        assert received.callback_reasons == {"apache": "never-started"}


class TestTheParentReadsItOffThatMessage:
    """Closing the loop: the parent's own handler, on this message."""

    def test_the_result_learns_what_was_attempted(self, queue):
        msg = QueueProgressMessage.from_json(
            queue._install_done_message(_Op(), 3, 3).to_json())
        result = OperationResult(operation_id="op-1",
                                 op_type=OperationType.INSTALL,
                                 success=True)

        _carry_attempts(result, msg)

        assert result.attempted == ["apache", "bash"]
        assert result.attempted_erases == ["obexd"]

    def test_a_later_empty_message_does_not_erase_it(self, queue):
        result = OperationResult(operation_id="op-1",
                                 op_type=OperationType.INSTALL,
                                 success=True)
        _carry_attempts(result, queue._install_done_message(_Op(), 3, 3))

        _carry_attempts(result, QueueProgressMessage(msg_type="progress"))

        assert result.attempted == ["apache", "bash"]
