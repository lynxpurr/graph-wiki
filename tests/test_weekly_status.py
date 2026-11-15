"""Machine-readable verdicts and explicit baseline migration (#46, #48)."""
import json
import errno

import pytest


def baseline():
    return {'scores': {'total': 80}, 'graph_stats': {'nodes': 2, 'edges': 2},
            'diagnosis_summary': {'by_severity': {'P0': 0, 'P1': 0, 'P2': 0}}}


def patch_tools(monkeypatch, weekly, *, score=80, error=None):
    monkeypatch.setattr(weekly, '_run_diagnose', lambda *args: (
        {'diagnosis': [], 'graph_stats': {'nodes': 2, 'edges': 2}}, error,
    ))
    monkeypatch.setattr(weekly, '_run_test_suite', lambda *args: ({'summary': {'score': score}}, None))


@pytest.mark.parametrize(('content', 'expected'), [
    (None, 'baseline_missing'), ('{bad', 'baseline_corrupt'),
    ('[]', 'baseline_invalid'), ('{}', 'baseline_invalid'),
    ('{"scores": []}', 'baseline_invalid'),
    ('{"scores": {"total": null}}', 'baseline_invalid'),
    ('{"scores": {"total": true}}', 'baseline_invalid'),
    ('{"scores": {"total": NaN}}', 'baseline_invalid'),
    (json.dumps({'scores': {'total': 10 ** 400}}), 'baseline_invalid'),
    ('{"graph_stats": {"nodes": "2"}}', 'baseline_invalid'),
    ('{"multi_source": {"D8": {"conflicts": "1"}}}', 'baseline_invalid'),
    ('{"diagnosis_summary": {"by_severity": {"P0": 0}}}', 'baseline_invalid'),
])
def test_bad_baseline_status_is_not_degradation(monkeypatch, load_script_module, tmp_path, content, expected):
    weekly = load_script_module('weekly-health-check.py')
    patch_tools(monkeypatch, weekly)
    if content is not None:
        (tmp_path / '.graph-baseline.json').write_text(content, encoding='utf-8')
    result = weekly.check_regression('wiki', wiki_root=str(tmp_path))
    assert result['status'] == expected
    assert result['pass'] is False
    assert result['errors']


@pytest.mark.parametrize(('score', 'error', 'expected'), [(80, None, 'ok'), (70, None, 'degraded'), (70, 'crash', 'execution_error')])
def test_status_precedence_keeps_execution_failure_visible(monkeypatch, load_script_module, tmp_path, score, error, expected):
    weekly = load_script_module('weekly-health-check.py')
    (tmp_path / '.graph-baseline.json').write_text(json.dumps(baseline()), encoding='utf-8')
    patch_tools(monkeypatch, weekly, score=score, error=error)
    result = weekly.check_regression('wiki', wiki_root=str(tmp_path))
    assert result['status'] == expected
    assert result['pass'] == (expected == 'ok')


def test_unreadable_baseline_has_its_own_status(monkeypatch, load_script_module, tmp_path):
    weekly = load_script_module('weekly-health-check.py')
    patch_tools(monkeypatch, weekly)
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, 'denied')
    monkeypatch.setattr(weekly, 'open', denied, raising=False)
    result = weekly.check_regression('wiki', wiki_root=str(tmp_path))
    assert result['status'] == 'baseline_unreadable'
    assert result['pass'] is False


def test_legacy_fallback_is_explicit_and_new_file_wins(monkeypatch, load_script_module, tmp_path):
    weekly = load_script_module('weekly-health-check.py')
    patch_tools(monkeypatch, weekly)
    (tmp_path / '.regression-baseline.json').write_text(json.dumps(baseline()), encoding='utf-8')
    legacy = weekly.check_regression('wiki', wiki_root=str(tmp_path))
    assert legacy['status'] == 'ok'  # Compatible fallback is allowed, but visible.
    assert legacy['baseline_source'] == 'legacy'
    assert legacy['warnings'] == [legacy['baseline_migration']]
    assert legacy['baseline_mtime']
    assert '正在读取旧基线' in weekly.format_report(legacy)
    (tmp_path / '.graph-baseline.json').write_text(json.dumps(baseline()), encoding='utf-8')
    primary = weekly.check_regression('wiki', wiki_root=str(tmp_path))
    assert primary['baseline_source'] == 'primary'
    assert primary['warnings'] == []
    assert primary['baseline_migration'] is None


def test_exit_zero_preserves_failure_status(monkeypatch, load_script_module, capsys):
    weekly = load_script_module('weekly-health-check.py')
    monkeypatch.setattr(weekly, 'check_regression', lambda **args: {'pass': False, 'status': 'baseline_corrupt'})
    assert weekly.main(['--json', '--exit-zero']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'baseline_corrupt'
