"""core/ in isolation. No Redis, no Django test client, no IO of any kind.

That these run at all is the point of the core/ ↔ adapters/ boundary: the arithmetic every waiter
sees is checkable against hand-written integers.
"""

import unittest

from core.keys import EventKeys
from core.state import JoinOutcome, QueueState, WaiterState, eta_seconds_for, position_from, state_from
from core.validation import is_valid_event_id, is_valid_queue_token, new_queue_token


class EventKeysTests(unittest.TestCase):
    def test_every_key_is_namespaced_to_its_event(self) -> None:
        keys = EventKeys("coldplay-mumbai-2026")
        self.assertEqual(keys.queue, "qf:coldplay-mumbai-2026:queue")
        self.assertEqual(keys.seq, "qf:coldplay-mumbai-2026:seq")
        self.assertEqual(keys.admitted, "qf:coldplay-mumbai-2026:admitted")
        self.assertEqual(keys.bucket, "qf:coldplay-mumbai-2026:bucket")
        self.assertEqual(keys.config, "qf:coldplay-mumbai-2026:config")
        self.assertEqual(keys.channel, "qf:coldplay-mumbai-2026:events")

    def test_two_events_share_no_key(self) -> None:
        """A shared key between events would let one drop's admissions move another's queue."""
        a, b = EventKeys("event-a"), EventKeys("event-b")
        for name in ("queue", "seq", "admitted", "bucket", "config", "channel"):
            self.assertNotEqual(getattr(a, name), getattr(b, name))


class ValidationTests(unittest.TestCase):
    def test_accepts_a_lowercase_slug(self) -> None:
        self.assertTrue(is_valid_event_id("coldplay-mumbai-2026"))
        self.assertTrue(is_valid_event_id("a"))

    def test_rejects_ids_that_are_not_slugs(self) -> None:
        for bad in ("", "Coldplay", "with_underscore", "-leading", "trailing-", "a--b", "a b", "a/b"):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_event_id(bad))

    def test_rejects_an_over_long_id(self) -> None:
        """64 matches booking_service's SlugField(max_length=64) — one id, both services."""
        self.assertTrue(is_valid_event_id("a" * 64))
        self.assertFalse(is_valid_event_id("a" * 65))

    def test_minted_tokens_are_valid_and_distinct(self) -> None:
        tokens = {new_queue_token() for _ in range(1000)}
        self.assertEqual(len(tokens), 1000, "a collision here means the token is not random")
        for token in tokens:
            self.assertTrue(is_valid_queue_token(token))
            self.assertEqual(len(token), 32)

    def test_rejects_malformed_tokens(self) -> None:
        for bad in ("", "abc", "g" * 32, "A" * 32, "0" * 31, "0" * 33, " " + "0" * 31):
            with self.subTest(bad=bad):
                self.assertFalse(is_valid_queue_token(bad))

    def test_uppercase_hex_is_rejected(self) -> None:
        """We mint lowercase, and 'am I already queued' is an exact-match ZSCORE. Accepting the
        uppercase spelling would make one token look like two and hand the holder a second
        place in the queue."""
        token = new_queue_token()
        self.assertTrue(is_valid_queue_token(token))
        self.assertFalse(is_valid_queue_token(token.upper()))


class PositionArithmeticTests(unittest.TestCase):
    def test_position_is_sequence_minus_admitted(self) -> None:
        self.assertEqual(position_from(sequence=24181, admitted_total=0), 24181)
        self.assertEqual(position_from(sequence=100, admitted_total=99), 1)

    def test_position_never_drops_below_one(self) -> None:
        """A position of 0 or less would mean more people were admitted than were ever
        sequenced. That is a bug upstream, and it must not reach a user as '0' or '-3'."""
        self.assertEqual(position_from(sequence=5, admitted_total=5), 1)
        self.assertEqual(position_from(sequence=5, admitted_total=99), 1)


class EtaTests(unittest.TestCase):
    def test_eta_is_position_over_rate_in_seconds(self) -> None:
        self.assertEqual(eta_seconds_for(position=100, rate_per_min=100), 60)
        self.assertEqual(eta_seconds_for(position=2000, rate_per_min=100), 1200)

    def test_eta_rounds_up(self) -> None:
        """Rounding down would promise a moment that has already passed by the time it renders."""
        self.assertEqual(eta_seconds_for(position=1, rate_per_min=100), 1)

    def test_paused_drop_has_no_eta(self) -> None:
        """rate 0 means the drop is paused (FR-13). None, not 0 — a 0 renders as 'any moment
        now', which is the exact opposite of the truth."""
        self.assertIsNone(eta_seconds_for(position=500, rate_per_min=0))
        self.assertIsNone(eta_seconds_for(position=500, rate_per_min=-1))


class StateAssemblyTests(unittest.TestCase):
    def test_state_from_builds_the_wire_shape(self) -> None:
        outcome = JoinOutcome(
            sequence=24181, joined=True, total_waiting=61004, admitted_total=0, rate_per_min=100
        )
        state = state_from(outcome)

        self.assertIsInstance(state, QueueState)
        self.assertEqual(
            state.as_dict(),
            {
                "position": 24181,
                "total_waiting": 61004,
                "admitted_total": 0,
                "eta_seconds": 14509,
                "state": WaiterState.WAITING,
            },
        )

    def test_state_serialises_as_a_plain_string(self) -> None:
        """WaiterState is a StrEnum, so json.dumps emits "waiting", not "WaiterState.WAITING"."""
        import json

        outcome = JoinOutcome(
            sequence=1, joined=True, total_waiting=1, admitted_total=0, rate_per_min=100
        )
        self.assertIn('"state": "waiting"', json.dumps(state_from(outcome).as_dict()))
