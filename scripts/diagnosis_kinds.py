"""Shared distinction between quality issues and informational diagnostics."""

KINDS_VERSION = 1
NON_ISSUE_KINDS = {'disclosure', 'assessment'}


def is_issue(item):
    # Legacy/unknown kinds remain issues: never silently hide an unknown finding.
    return item.get('kind') not in NON_ISSUE_KINDS


def severity_counts(items, *, issues_only=False):
    return {
        severity: sum(
            item.get('severity') == severity
            for item in items if not issues_only or is_issue(item)
        )
        for severity in ('P0', 'P1', 'P2')
    }
