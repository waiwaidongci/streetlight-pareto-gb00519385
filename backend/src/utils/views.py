from src.utils.time import iso


def dataset(item):
    return {'id': item.id, 'name': item.name, 'filename': item.filename, 'row_count': item.row_count,
            'checksum': item.checksum, 'source': item.source, 'batch_date': item.batch_date,
            'author': item.author, 'created_at': iso(item.created_at)}


def run(item, detail=False):
    item_view = {'id': item.id, 'dataset_id': item.dataset_id, 'name': item.name, 'status': item.status,
                 'author': item.author, 'review': item.review, 'decision': item.decision,
                 'created_at': iso(item.created_at), 'finished_at': iso(item.finished_at),
                 'scheduled_at': iso(item.scheduled_at), 'schedule_id': item.schedule_id,
                 'report_day': item.report_day,
                 'roster_dataset_id': item.roster_dataset_id,
                 'hotline_dataset_id': item.hotline_dataset_id,
                 'has_export': bool(item.export_file), 'error': item.error}
    if detail:
        item_view.update(params=item.params, result=item.result, review_note=item.review_note)
    return item_view


def schedule(item):
    return {'id': item.id, 'name': item.name, 'dataset_id': item.dataset_id, 'recipient': item.recipient,
            'next_at': iso(item.next_at), 'repeat_daily': item.repeat_daily, 'enabled': item.enabled,
            'anchor_report_day': item.anchor_report_day, 'report_offset': item.report_offset,
            'next_report_day': item.anchor_report_day and item.enabled
            and _shift(item.anchor_report_day, item.report_offset)}


def _shift(anchor_day, offset):
    from datetime import date, timedelta
    return (date.fromisoformat(anchor_day) + timedelta(days=offset)).isoformat()


def delivery(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'state': item.state, 'detail': item.detail, 'attempts': item.attempts,
            'created_at': iso(item.created_at), 'updated_at': iso(item.updated_at)}
