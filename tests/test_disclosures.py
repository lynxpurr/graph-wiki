"""Disclosure visibility must not generate quality-regression alerts (#47)."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(script, wiki, *extra):
    return subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / script), '--source', 'wiki',
         '--wiki-root', str(wiki), *extra],
        capture_output=True, text=True, timeout=60,
    )


def baseline(tmp_path):
    wiki = tmp_path / 'wiki'
    wiki.mkdir()
    (wiki / 'a.md').write_text('# A\n[[b]]\n', encoding='utf-8')
    (wiki / 'b.md').write_text('# B\n[[a]]\n', encoding='utf-8')
    saved = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/wiki_analyzer.py'), 'baseline-save',
         '--source', 'wiki', '--wiki-root', str(wiki)],
        capture_output=True, text=True, timeout=60,
    )
    assert saved.returncode == 0, saved.stderr
    return wiki


@pytest.mark.parametrize('content', ['\n```\n[[example]]\n```\n', '\n[[skill:example]]\n', '\n[[   ]]\n'])
def test_first_disclosure_is_visible_without_regression(tmp_path, content):
    wiki = baseline(tmp_path)
    before = json.loads(run('graph-diagnose.py', wiki, '--json').stdout)
    with (wiki / 'a.md').open('a', encoding='utf-8') as handle:
        handle.write(content)
    after = json.loads(run('graph-diagnose.py', wiki, '--json').stdout)
    assert after['diagnosis_summary']['by_severity']['P2'] == before['diagnosis_summary']['by_severity']['P2'] + 1
    assert after['diagnosis_summary']['issue_by_severity'] == before['diagnosis_summary']['issue_by_severity']
    assert any(item['kind'] == 'disclosure' for item in after['disclosures'])
    checked = run('weekly-health-check.py', wiki, '--json')
    result = json.loads(checked.stdout)
    assert checked.returncode == 0, result
    assert result['pass'] is True
    assert result['alerts'] == []


def test_disclosure_does_not_hide_real_broken_link(tmp_path):
    wiki = baseline(tmp_path)
    with (wiki / 'a.md').open('a', encoding='utf-8') as handle:
        handle.write('\n[[missing-page]]\n\n```\n[[example]]\n```\n')
    checked = run('weekly-health-check.py', wiki, '--json')
    result = json.loads(checked.stdout)
    assert checked.returncode == 1
    assert any('P0 问题增加' in alert for alert in result['alerts'])


def test_legacy_counter_cannot_silently_mix_with_issue_counter(tmp_path):
    wiki = baseline(tmp_path)
    path = wiki / '.graph-baseline.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['diagnosis_summary'].pop('kind_version')
    data['diagnosis_summary'].pop('issue_by_severity')
    path.write_text(json.dumps(data), encoding='utf-8')
    checked = run('weekly-health-check.py', wiki, '--json')
    result = json.loads(checked.stdout)
    assert checked.returncode == 1
    assert any('重新 baseline-save' in error for error in result['errors'])


def test_health_metrics_ignore_information_but_keep_unknown_findings():
    from scorecard import _diagnosis_counts
    from diagnosis_kinds import severity_counts
    items = [
        {'dimension': 'D4', 'severity': 'P2', 'kind': 'disclosure'},
        {'dimension': 'D6', 'severity': 'P2', 'kind': 'assessment'},
        {'dimension': 'D4', 'severity': 'P0', 'kind': 'future-kind'},
    ]
    assert _diagnosis_counts({'diagnosis': items}) == ({'D4': 1}, True)
    assert severity_counts(items, issues_only=True) == {'P0': 1, 'P1': 0, 'P2': 0}
