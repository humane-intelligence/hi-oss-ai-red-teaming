"""Tests for `app.core.evaluations.services.tag_keys` — the tagging policy, service layer.

The two entry points are deliberately asymmetric: `assert_tags_allowed` rejects what a request
authors, `allowed_tags_only` filters what a stored map may put in the prompt. Both branch on the
same `(tags_enabled, tags_restricted)` pair, so the matrix lives here rather than being driven
through SSE round-trips at the API layer — those keep one wiring representative per verb, which is
the part a service test cannot see.

Mixed tiers, so the marker is per group rather than per module: `TagFoldPolicy` is a pure value
object and stays in the unit run, everything touching `db_session` is `integration`.
"""

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.conversations.schemas import MAX_TAGS
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.services.tag_keys import TagFoldPolicy
from app.core.evaluations.services.tag_keys import add_tag_key
from app.core.evaluations.services.tag_keys import allowed_tag_keys
from app.core.evaluations.services.tag_keys import allowed_tags_only
from app.core.evaluations.services.tag_keys import assert_tags_allowed
from app.core.evaluations.services.tag_keys import list_tag_keys
from app.core.evaluations.services.tag_keys import load_tag_fold_policy
from app.core.evaluations.services.tag_keys import remove_tag_key
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.helpers import sanitise_single_line
from tests.conftest import persist_evaluation_group


async def _evaluation(
    db_session: AsyncSession, *, enabled: bool = True, restricted: bool = False, keys: list[str] | None = None
) -> Evaluation:
    """An evaluation with the tagging policy under test, plus any allowed keys."""
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title="E",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        tags_enabled=enabled,
        tags_restricted=restricted,
    )
    db_session.add(evaluation)
    await db_session.flush()
    for key in keys or []:
        db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key=key))
    await db_session.flush()
    return evaluation


class TestAllowedTagsOnly:
    """What a stored map may put in the prompt."""

    pytestmark = pytest.mark.integration

    async def test_empty_map_needs_no_policy(self, db_session: AsyncSession) -> None:
        # The short-circuit is what this pins, so the policy read has to be observably unavailable:
        # against a soft-deleted evaluation the read fails closed, so returning `{}` can only be the
        # early return.
        evaluation = await _evaluation(db_session)
        evaluation.soft_delete(None)
        await db_session.flush()

        assert await allowed_tags_only(db_session, evaluation.id, {}) == {}

    async def test_unrestricted_tagging_passes_every_key(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session)

        assert await allowed_tags_only(db_session, evaluation.id, {"env": "prod", "persona": "auditor"}) == {
            "env": "prod",
            "persona": "auditor",
        }

    async def test_disabled_tagging_drops_every_key(self, db_session: AsyncSession) -> None:
        # Dropped, not rejected: the map carries stored tags, so rejecting would wedge every later
        # turn of a conversation tagged before the flag was flipped.
        evaluation = await _evaluation(db_session, enabled=False)

        assert await allowed_tags_only(db_session, evaluation.id, {"env": "prod"}) == {}

    async def test_restricted_tagging_keeps_only_allowed_keys(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, restricted=True, keys=["env"])

        assert await allowed_tags_only(db_session, evaluation.id, {"env": "prod", "persona": "auditor"}) == {
            "env": "prod"
        }

    async def test_restricted_tagging_with_no_allowed_keys_drops_everything(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, restricted=True)

        assert await allowed_tags_only(db_session, evaluation.id, {"env": "prod"}) == {}

    async def test_a_removed_key_stops_reaching_the_prompt(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, restricted=True, keys=["env"])
        await remove_tag_key(db_session, evaluation_id=evaluation.id, key="env", by_id=uuid4())

        assert await allowed_tags_only(db_session, evaluation.id, {"env": "prod"}) == {}

    async def test_a_union_over_one_layer_cap_is_not_truncated(self, db_session: AsyncSession) -> None:
        # No ceiling on the merged map: each layer is bounded at the schema edge, so their union is
        # too. Pinned because a truncation here would have to discard the layer the contract says
        # wins.
        evaluation = await _evaluation(db_session)
        merged = {f"c{i:02d}": "v" for i in range(MAX_TAGS)} | {f"m{i:02d}": "v" for i in range(MAX_TAGS)}

        assert await allowed_tags_only(db_session, evaluation.id, merged) == merged

    async def test_a_vanished_evaluation_fails_closed(self, db_session: AsyncSession) -> None:
        # Fail closed rather than "no policy, allow everything": every caller resolved a live parent
        # first, so a missing row means the caller's assumptions are already broken.
        evaluation = await _evaluation(db_session)
        evaluation.soft_delete(None)
        await db_session.flush()

        with pytest.raises(NotFoundError):
            await allowed_tags_only(db_session, evaluation.id, {"env": "prod"})


class TestAssertTagsAllowed:
    """What a request may author."""

    pytestmark = pytest.mark.integration

    async def test_authoring_nothing_is_allowed_even_with_tagging_disabled(self, db_session: AsyncSession) -> None:
        # The send path passes only `body.tags`, so an ordinary message on a tagged conversation must
        # not be refused just because the evaluation stopped allowing tags.
        evaluation = await _evaluation(db_session, enabled=False)

        await assert_tags_allowed(db_session, evaluation.id, {})

    async def test_authoring_with_tagging_disabled_is_refused(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, enabled=False)

        with pytest.raises(BadRequestError, match="disabled"):
            await assert_tags_allowed(db_session, evaluation.id, {"env": "prod"})

    async def test_authoring_is_free_form_while_unrestricted(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, keys=["env"])  # keys are inert until restricted

        await assert_tags_allowed(db_session, evaluation.id, {"anything": "goes"})

    async def test_authoring_an_allowed_key_while_restricted_passes(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, restricted=True, keys=["env", "persona"])

        await assert_tags_allowed(db_session, evaluation.id, {"env": "prod"})

    async def test_authoring_a_disallowed_key_names_every_offender(self, db_session: AsyncSession) -> None:
        # Names them so the caller can fix the row; sorted, so the message is stable to compare.
        evaluation = await _evaluation(db_session, restricted=True, keys=["env"])

        with pytest.raises(BadRequestError, match="persona, zone"):
            await assert_tags_allowed(db_session, evaluation.id, {"env": "prod", "zone": "eu", "persona": "auditor"})

    async def test_authoring_against_a_vanished_evaluation_fails_closed(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session)
        evaluation.soft_delete(None)
        await db_session.flush()

        with pytest.raises(NotFoundError):
            await assert_tags_allowed(db_session, evaluation.id, {"env": "prod"})


class TestAllowList:
    """The allow-list itself — add / remove / list."""

    pytestmark = pytest.mark.integration

    async def test_added_key_is_listed_and_allowed(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session)

        row = await add_tag_key(db_session, evaluation_id=evaluation.id, key="env")

        assert row.key == "env"
        assert [r.key for r in await list_tag_keys(db_session, evaluation.id)] == ["env"]
        assert await allowed_tag_keys(db_session, evaluation.id) == {"env"}

    async def test_adding_the_same_key_twice_conflicts(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, keys=["env"])

        with pytest.raises(ConflictError, match="already allowed"):
            await add_tag_key(db_session, evaluation_id=evaluation.id, key="env")

    async def test_adding_past_the_cap_conflicts(self, db_session: AsyncSession) -> None:
        # The ceiling is a conversation's own tag-map cap: a longer allow-list is weight the write
        # path re-reads with no conversation able to use it.
        evaluation = await _evaluation(db_session, keys=[f"k{i:02d}" for i in range(MAX_TAGS)])

        with pytest.raises(ConflictError, match="maximum"):
            await add_tag_key(db_session, evaluation_id=evaluation.id, key="one-more")

    async def test_a_key_can_be_re_added_after_removal(self, db_session: AsyncSession) -> None:
        # The whole reason the unique index is partial on `deleted_at IS NULL` — and the reason the
        # pre-read has to be live-filtered. An unfiltered pre-read would 409 here.
        evaluation = await _evaluation(db_session, keys=["env"])
        await remove_tag_key(db_session, evaluation_id=evaluation.id, key="env", by_id=uuid4())

        await add_tag_key(db_session, evaluation_id=evaluation.id, key="env")

        assert [r.key for r in await list_tag_keys(db_session, evaluation.id)] == ["env"]

    async def test_a_removed_key_frees_a_slot_under_the_cap(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, keys=[f"k{i:02d}" for i in range(MAX_TAGS)])
        await remove_tag_key(db_session, evaluation_id=evaluation.id, key="k00", by_id=uuid4())

        await add_tag_key(db_session, evaluation_id=evaluation.id, key="fresh")

        assert "fresh" in await allowed_tag_keys(db_session, evaluation.id)

    async def test_removing_an_undefined_key_is_not_found(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session)

        with pytest.raises(NotFoundError, match="not defined"):
            await remove_tag_key(db_session, evaluation_id=evaluation.id, key="env", by_id=uuid4())

    async def test_soft_deleted_keys_leave_the_allowed_set(self, db_session: AsyncSession) -> None:
        evaluation = await _evaluation(db_session, keys=["env", "persona"])

        await remove_tag_key(db_session, evaluation_id=evaluation.id, key="env", by_id=uuid4())

        assert await allowed_tag_keys(db_session, evaluation.id) == {"persona"}
        assert [r.key for r in await list_tag_keys(db_session, evaluation.id)] == ["persona"]


class TestTagFoldPolicy:
    """`TagFoldPolicy.unsent` — the read side's offline mirror of `allowed_tags_only`."""

    pytestmark = pytest.mark.unit  # pure function — no DB, so it stays out of the integration tier

    def test_tagging_off_means_nothing_reaches_the_model(self) -> None:
        policy = TagFoldPolicy(enabled=False, restricted=False, allowed=frozenset())
        assert policy.unsent({"env": "prod", "team": "red"}) == ["env", "team"]

    def test_unrestricted_sends_every_filled_tag(self) -> None:
        policy = TagFoldPolicy(enabled=True, restricted=False, allowed=frozenset())
        assert policy.unsent({"env": "prod"}) == []

    def test_restricted_drops_keys_outside_the_allowed_set(self) -> None:
        policy = TagFoldPolicy(enabled=True, restricted=True, allowed=frozenset({"env"}))
        assert policy.unsent({"env": "prod", "legacy": "x"}) == ["legacy"]

    def test_a_value_carrying_no_text_is_never_sent(self) -> None:
        # `render_tag_context` skips it, so the fold drops it whatever the key policy says — and the
        # write edge normalises such a value to empty, which is the shape this catches.
        policy = TagFoldPolicy(enabled=True, restricted=False, allowed=frozenset())
        assert policy.unsent({"unfilled": "", "blank": "   ", "real": "v"}) == ["blank", "unfilled"]

    def test_a_non_string_value_is_judged_rather_than_crashing(self) -> None:
        # A map written by an import or direct SQL need not hold strings, and `render_tag_context`
        # coerces — so an export judging the same map must not be the thing that dies on one.
        policy = TagFoldPolicy(enabled=True, restricted=False, allowed=frozenset())
        # `cast` rather than a plain literal: the annotation says this cannot happen, and the point is
        # that storage lets it, so the test has to say "yes, deliberately" out loud.
        assert policy.unsent(cast("dict[str, str]", {"n": 5})) == []

    def test_an_empty_map_reports_nothing(self) -> None:
        # Restricted with nothing allowed is the configuration that reports *every* key, so this pins
        # the boundary rather than the short-circuit (which no configuration can distinguish).
        policy = TagFoldPolicy(enabled=True, restricted=True, allowed=frozenset())
        assert policy.unsent({}) == []
        assert policy.unsent({"env": "prod"}) == ["env"]


@pytest.mark.integration
async def test_load_tag_fold_policy_reads_the_allow_list_only_when_restricted(db_session: AsyncSession) -> None:
    # Unrestricted evaluations don't need the key list, so the loader skips that query — and an empty
    # `allowed` on an unrestricted policy must not read as "nothing is allowed".
    evaluation = await _evaluation(db_session, restricted=False, keys=["env"])

    unrestricted = await load_tag_fold_policy(db_session, evaluation.id)
    assert unrestricted.allowed == frozenset()
    assert unrestricted.unsent({"anything": "v"}) == []

    evaluation.tags_restricted = True
    db_session.add(evaluation)
    await db_session.flush()

    restricted = await load_tag_fold_policy(db_session, evaluation.id)
    assert restricted.allowed == frozenset({"env"})
    assert restricted.unsent({"anything": "v"}) == ["anything"]


class TestPolicyMirrorAgreesWithTheFold:
    """The read-side mirror and the fold must answer the same question the same way.

    The rule lives in two places on purpose — `allowed_tags_only` runs at the fold with a session,
    `TagFoldPolicy.unsent` answers per row offline — so the relation between them is asserted here
    rather than restated as two hand-written expectations. A tag reaches the model when the fold keeps
    it **and** `render_tag_context` finds text in its value; `unsent` is exactly the complement.
    """

    pytestmark = pytest.mark.integration

    @pytest.mark.parametrize(
        ("enabled", "restricted", "keys", "tags"),
        [
            (True, False, [], {"env": "prod", "persona": "auditor"}),
            (True, False, [], {"env": "prod", "unfilled": "", "invisible": "\u200b"}),
            (True, True, ["env"], {"env": "prod", "legacy": "x"}),
            (True, True, ["env"], {"env": "", "legacy": "x"}),
            (True, True, [], {"env": "prod"}),
            (False, False, [], {"env": "prod", "unfilled": ""}),
            (True, False, [], {}),
        ],
        ids=[
            "unrestricted-all-filled",
            "unrestricted-blank-and-invisible",
            "restricted-one-key-outside",
            "restricted-allowed-key-blank",
            "restricted-nothing-allowed",
            "tagging-disabled",
            "empty-map",
        ],
    )
    async def test_unsent_is_the_complement_of_what_the_fold_sends(
        self, db_session: AsyncSession, enabled: bool, restricted: bool, keys: list[str], tags: dict[str, str]
    ) -> None:
        evaluation = await _evaluation(db_session, enabled=enabled, restricted=restricted, keys=keys)

        folded = await allowed_tags_only(db_session, evaluation.id, tags)
        policy = await load_tag_fold_policy(db_session, evaluation.id)

        reaches_model = {key for key, value in folded.items() if sanitise_single_line(str(value))}
        assert policy.unsent(tags) == sorted(set(tags) - reaches_model)
