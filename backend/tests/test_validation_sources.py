import pytest
from src.domain.analysis import validate_rows
from src.services.validation import parse_rows


def _roster_row(site_id='LH-01'):
    return {'record_type': '灯组台账', 'site_id': site_id, 'district': '滨河片区',
            'site_name': '柳汀大道', 'fixture_count': 10, 'incident_id': None, 'report_date': None}


def _incident_row(site_id='LH-01', incident_id='LT-1'):
    return {'record_type': '暗区工单', 'site_id': site_id, 'district': None, 'site_name': None,
            'fixture_count': None, 'incident_id': incident_id, 'report_date': '2026-11-14'}


def test_hotline_only_batch_allowed_without_roster():
    rows = [_incident_row(), _incident_row(incident_id='LT-2')]
    records = validate_rows(rows, source='hotline')
    assert len(records) == 2
    assert all(r['record_type'] == '暗区工单' for r in records)


def test_roster_only_batch_allowed():
    rows = [_roster_row('LH-01'), _roster_row('LH-02')]
    records = validate_rows(rows, source='roster')
    assert {r['site_id'] for r in records} == {'LH-01', 'LH-02'}


def test_source_mismatch_rejected():
    with pytest.raises(ValueError, match='热线批次'):
        validate_rows([_roster_row()], source='hotline')
    with pytest.raises(ValueError, match='灯组名册批次'):
        validate_rows([_incident_row()], source='roster')


def test_combined_batch_still_requires_matching_sites():
    rows = [_roster_row('LH-01'), _incident_row('LH-99')]
    with pytest.raises(ValueError, match='未匹配灯组'):
        validate_rows(rows)


def test_parse_rows_passes_source():
    header = 'record_type,site_id,district,site_name,fixture_count,incident_id,report_date'
    line = '暗区工单,LH-01,,,,LT-9,2026-11-14'
    rows = parse_rows(f'{header}\n{line}\n', source='hotline')
    assert rows[0]['incident_id'] == 'LT-9'
