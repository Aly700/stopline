from datetime import timedelta

from stopline.voice.sentences import sentence_for_baseline, sentences_for_receipt


def test_demo_open_receipt(receipt_records):
    assert sentences_for_receipt(*receipt_records) == [
        'Stop confirmed. One completed. One unknown. Two blocked.',
        'The receipt is open.',
    ]


def test_demo_closing_receipt(receipt_records):
    receipt, effects, refusals = receipt_records
    receipt['status'] = 'closed'
    effects[1]['state'] = 'applied'
    assert sentences_for_receipt(receipt, effects, refusals, closing=True) == [
        'The router answered. The traffic shift was applied.',
        'The receipt is closed.',
    ]


def test_rejected_shift_does_not_claim_it_applied(receipt_records):
    receipt, effects, refusals = receipt_records
    receipt['status'] = 'closed'
    effects[1]['state'] = 'rejected'
    assert ' '.join(sentences_for_receipt(receipt, effects, refusals, closing=True)) == (
        'The router answered. The traffic shift was refused. The receipt is closed.'
    )


def test_counts_only_covered_and_distinct_refused_actions(receipt_records):
    receipt, effects, refusals = receipt_records
    effects.append(dict(_id='unrelated', state='applied'))
    deny = refusals[0]
    refusals += [dict(deny), dict(deny, action_id='restart'),
                 dict(deny, action_id='before', at=receipt['stopped_at'] - timedelta(seconds=1)),
                 dict(deny, action_id='other-run', run_id='r2'),
                 dict(deny, action_id='other-branch', mandate_id='elsewhere'),
                 dict(deny, action_id='allowed', kind='allow')]
    assert ' '.join(sentences_for_receipt(receipt, effects, refusals)) == (
        'Stop confirmed. One completed. One unknown. Two blocked. The receipt is open.'
    )


def test_missing_covered_action_is_unknown(receipt_records):
    receipt, effects, refusals = receipt_records
    assert 'Two unknown.' in ' '.join(sentences_for_receipt(receipt, effects[1:], refusals))


def test_baseline():
    assert sentence_for_baseline(1) == 'Stop pressed. One action ran after the stop.'
    assert sentence_for_baseline(2) == 'Stop pressed. Two actions ran after the stop.'
    assert sentence_for_baseline(0) == 'Stop pressed. No completed action after the stop.'
