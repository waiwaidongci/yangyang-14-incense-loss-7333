from src.utils.formatting import timestamp


def review_view(item):
    return {'decision': item.decision, 'note': item.note, 'reviewer': item.reviewer,
            'at': timestamp(item.created_at)} if item else None


def run_view(item, review=None, *, include_result=False):
    view = {'id': item.id, 'title': item.title, 'owner': item.owner, 'status': item.status,
            'parameters': item.parameters, 'sample_size': item.sample_size,
            'snapshot_id': item.last_observation_id,
            'last_voucher_id': item.last_voucher_id, 'voucher_nos': list(item.voucher_nos or []),
            'created_at': timestamp(item.created_at),
            'finished_at': timestamp(item.finished_at), 'review': review_view(review), 'error': item.error}
    if include_result:
        view['result'] = item.result
    return view


def observation_view(item):
    return {'id': item.id, 'data': item.payload, 'created_at': timestamp(item.created_at)}


def voucher_view(item):
    return {
        'id': item.id, 'voucher_no': item.voucher_no, 'source_batch': item.source_batch,
        'target_batch': item.target_batch, 'amount_cents': item.amount_cents, 'reason': item.reason,
        'status': item.status, 'attempts': item.attempts, 'last_error': item.last_error,
        'created_by': item.created_by, 'created_at': timestamp(item.created_at),
        'posted_at': timestamp(item.posted_at),
    }


def account_view(item):
    return {'id': item.id, 'batch_code': item.batch_code, 'balance_cents': item.balance_cents,
            'version': item.version, 'updated_at': timestamp(item.updated_at)}


def entry_view(item):
    return {'id': item.id, 'voucher_no': item.voucher_no, 'batch_code': item.batch_code,
            'side': item.side, 'amount_cents': item.amount_cents,
            'balance_after': item.balance_after, 'created_at': timestamp(item.created_at)}


def schedule_view(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'interval_minutes': item.interval_minutes, 'next_at': timestamp(item.next_at), 'active': item.active}


def delivery_view(delivery, schedule):
    return {'id': delivery.id, 'schedule_id': schedule.id, 'recipient': schedule.recipient,
            'run_id': schedule.run_id, 'at': timestamp(delivery.delivered_at),
            'scheduled_at': timestamp(delivery.scheduled_at)}
