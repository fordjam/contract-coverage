#!/usr/bin/env python3
"""Portable futures support analysis. Python 3.11+, standard library only.

Run --example for canonical input, --self-test for regression tests, --help for
CLI policy overrides. analyze(payload) never performs I/O. Sessions are supplied
exchange trading days, and daily rows are individual outright/spread volumes.
"""
from __future__ import annotations

import argparse
import copy
import csv
import itertools
import json
import math
import statistics
import sys
import unittest
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path


class ValidationError(ValueError):
    """Invalid or ambiguous supplied data; messages identify the affected field."""


def fail(message):
    raise ValidationError(message)


def number(value, field, nullable=False, config=None):
    config = config or {}
    if value is None or (isinstance(value, str) and value in config.get('null_markers', [])):
        if nullable:
            return None
        fail(f'{field}: a number is required')
    if isinstance(value, bool):
        fail(f'{field}: boolean is not a quantity')
    if isinstance(value, str):
        thousands = config.get('thousands_separator')
        decimal = config.get('decimal_separator', '.')
        if thousands and thousands == decimal:
            fail('normalization: decimal and thousands separators must differ')
        if thousands:
            value = value.replace(thousands, '')
        if decimal != '.':
            value = value.replace(decimal, '.')
    try:
        result = float(value)
    except (ValueError, TypeError):
        fail(f'{field}: invalid number {value!r}')
    if not math.isfinite(result) or result < 0:
        fail(f'{field}: must be finite and nonnegative')
    return result


def day(value, field, config=None):
    if not isinstance(value, str):
        fail(f'{field}: expected a date string')
    formats = ['%Y-%m-%d'] + (config or {}).get('date_formats', [])
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
            if fmt == '%Y-%m-%d' and parsed.isoformat() != value:
                continue
            return parsed.isoformat()
        except ValueError:
            pass
    fail(f'{field}: invalid date {value!r}; use ISO YYYY-MM-DD or configure date_formats')


def instant(value, field):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            fail(f'{field}: timezone offset is required')
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        fail(f'{field}: expected ISO timestamp with timezone')


def availability(record):
    values = [instant(record[k], k) for k in ('available_at', 'published_at') if record.get(k)]
    return max(values) if values else None


def month(value, field):
    try:
        parsed = date.fromisoformat(str(value) + '-01')
        if parsed.strftime('%Y-%m') != value:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        fail(f'{field}: expected YYYY-MM')


def month_number(value):
    return int(value[:4]) * 12 + int(value[5:7])


def days_between(a, b):
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def mapped(row, mapping):
    result = dict(row)
    for canonical, source in mapping.items():
        if source in row:
            result[canonical] = row[source]
    return result


DEFAULTS = {
    'lookback_sessions': 60, 'start_date': None, 'analysis_date': None,
    'decision_time': None, 'min_adv': 0.0, 'min_positive_frequency': 0.0,
    'min_data_completeness': 1.0, 'min_oi': None, 'max_oi_age_days': None,
    'require_spread_legs': False, 'require_delivery_eligible': False,
    'require_notice_eligible': False, 'exclude_on_or_after_first_notice': False,
    'min_days_to_last_trade': None, 'costs': {}, 'capacities': {},
    'resource_usage': {}, 'exact_search_limit': 18, 'allow_heuristic': False,
    'conditional_rules': [],
}


def policy_for(payload):
    supplied = payload.get('policy')
    if supplied is not None and not isinstance(supplied, dict):
        fail('policy: expected an object')
    unknown = set(supplied or {}) - (set(DEFAULTS) | {'mode', 'budgets', 'targets', 'shared_budget', 'weights'})
    if unknown:
        fail(f'policy: unknown controls {sorted(unknown)}')
    policy = copy.deepcopy(DEFAULTS)
    policy.update(supplied or {})
    for key in ('costs', 'capacities', 'resource_usage', 'budgets', 'targets', 'weights'):
        if key in policy and not isinstance(policy[key], dict):
            fail(f'policy.{key}: expected an object')
    if not isinstance(policy['conditional_rules'], list):
        fail('policy.conditional_rules: expected a list of explicit rules')
    for rule in policy['conditional_rules']:
        if not isinstance(rule, dict) or not isinstance(rule.get('name'), str) or not isinstance(rule.get('delivery_months'), list):
            fail('conditional_rules: each rule needs name and delivery_months (month numbers)')
        if any(isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12 for m in rule['delivery_months']):
            fail('conditional_rules.delivery_months: use integer months 1 through 12')
        rule['min_known_sessions'] = number(rule.get('min_known_sessions', 20), 'conditional_rules.min_known_sessions')
        rule.setdefault('min_years', 1)
        if isinstance(rule['min_years'], bool) or not isinstance(rule['min_years'], int) or rule['min_years'] < 1:
            fail('conditional_rules.min_years: expected a positive integer')
    # These keys are allowed outside policy for metrics-only runs.
    for key in ('analysis_date', 'start_date', 'lookback_sessions', 'decision_time'):
        if key in payload and key not in (supplied or {}):
            policy[key] = payload[key]
    for key in ('lookback_sessions', 'exact_search_limit'):
        if isinstance(policy[key], bool) or not isinstance(policy[key], int) or policy[key] < 1:
            fail(f'policy.{key}: expected a positive integer')
    if policy['exact_search_limit'] > 24:
        fail('policy.exact_search_limit: maximum is 24 (exponential search)')
    for key in ('min_adv', 'min_positive_frequency', 'min_data_completeness'):
        policy[key] = number(policy[key], f'policy.{key}')
    for key in ('min_positive_frequency', 'min_data_completeness'):
        if policy[key] > 1:
            fail(f'policy.{key}: must be between 0 and 1')
    for key in ('min_oi', 'max_oi_age_days', 'min_days_to_last_trade'):
        if policy[key] is not None:
            policy[key] = number(policy[key], f'policy.{key}')
    for key in ('require_spread_legs', 'require_delivery_eligible', 'require_notice_eligible',
                'exclude_on_or_after_first_notice', 'allow_heuristic'):
        if not isinstance(policy[key], bool):
            fail(f'policy.{key}: expected boolean')
    if supplied is not None:
        if policy.get('mode') not in ('budget', 'target'):
            fail('policy.mode: expected budget or target; omit policy for metrics only')
        field = 'budgets' if policy['mode'] == 'budget' else 'targets'
        if field not in policy and not (field == 'budgets' and 'shared_budget' in policy):
            fail(f'policy.{field}: provide category constraints')
        for collection in ('budgets', 'targets'):
            for category, value in policy.get(collection, {}).items():
                if category not in ('outrights', 'spreads'):
                    fail(f'policy.{collection}: unknown category {category}')
                policy[collection][category] = number(value, f'policy.{collection}.{category}')
                if collection == 'targets' and policy[collection][category] > 1:
                    fail('policy.targets: use fractions between 0 and 1')
        if 'shared_budget' in policy:
            policy['shared_budget'] = number(policy['shared_budget'], 'policy.shared_budget')
            if set(policy.get('weights', {})) != {'outrights', 'spreads'}:
                fail('shared_budget requires explicit weights for outrights and spreads')
        weights = policy.setdefault('weights', {'outrights': 1.0, 'spreads': 1.0})
        if set(weights) != {'outrights', 'spreads'}:
            fail('policy.weights: specify outrights and spreads')
        for category in weights:
            weights[category] = number(weights[category], f'policy.weights.{category}')
        if not any(weights.values()):
            fail('policy.weights: at least one weight must be positive')
    for key in ('costs', 'capacities'):
        for identity, value in policy[key].items():
            policy[key][identity] = number(value, f'policy.{key}.{identity}')
    for identity, resources in policy['resource_usage'].items():
        if not isinstance(resources, dict):
            fail(f'resource_usage.{identity}: expected an object of resource quantities')
        for resource, value in resources.items():
            if resource not in policy['capacities']:
                fail(f'resource_usage.{identity}: {resource} has no capacity')
            resources[resource] = number(value, f'resource_usage.{identity}.{resource}')
    return policy, supplied is not None


def normalize(payload, policy):
    if not isinstance(payload, dict):
        fail('payload: expected an object')
    for key in ('exchange', 'family_id', 'sessions', 'daily', 'volume_basis', 'volume_unit'):
        if key not in payload:
            fail(f'{key}: required batch field')
    for key in ('exchange', 'family_id'):
        if not isinstance(payload[key], str) or not payload[key].strip():
            fail(f'{key}: expected a nonempty identifier')
    for key in ('sessions', 'daily', 'instruments', 'instrument_snapshots'):
        if key in payload and not isinstance(payload[key], list):
            fail(f'{key}: expected a list')
    for key in ('metadata', 'normalization', 'field_mapping'):
        if key in payload and not isinstance(payload[key], dict):
            fail(f'{key}: expected an object')
    if payload['volume_basis'] not in ('contract_month_total', 'direct_outright', 'unknown'):
        fail('volume_basis: unsupported definition')
    if payload['volume_unit'] != 'contracts':
        fail('volume_unit: normalized unit must be contracts; configure source conversion')
    config = payload.get('normalization', {})
    sessions = [day(s, 'sessions', config) for s in payload['sessions']]
    if not sessions or sessions != sorted(set(sessions)):
        fail('sessions: must be nonempty, ordered, unique exchange trading dates')
    policy['analysis_date'] = day(policy['analysis_date'] or sessions[-1], 'analysis_date', config)
    if policy['analysis_date'] not in sessions:
        fail('analysis_date: must be a supplied exchange session')
    if policy['start_date']:
        policy['start_date'] = day(policy['start_date'], 'start_date', config)
        if policy['start_date'] > policy['analysis_date']:
            fail('start_date: after analysis_date')
    cutoff = instant(policy['decision_time'], 'decision_time') if policy['decision_time'] else None
    metadata = {'catalog_complete': False, 'sessions_complete': False,
                'reports_complete': False, 'absent_rows_mean_zero': False}
    metadata.update(copy.deepcopy(payload.get('metadata', {})))
    for key in ('catalog_complete', 'sessions_complete', 'reports_complete', 'absent_rows_mean_zero'):
        if not isinstance(metadata[key], bool):
            fail(f'metadata.{key}: expected boolean')
    diag = {'discarded_unavailable': [], 'resolved_revisions': [], 'missing_rows': [],
            'zero_imputations': [], 'pit_missing_timestamps': [], 'mapping_errors': []}
    field_maps = payload.get('field_mapping', {})
    raw_catalog = payload.get('instruments')
    snapshots = None
    if raw_catalog is None:
        raw_catalog = []
        snapshots = []
        for snapshot in payload.get('instrument_snapshots', []):
            if not isinstance(snapshot, dict):
                fail('instrument_snapshots: expected snapshot objects')
            sd = day(snapshot.get('trade_date'), 'snapshot.trade_date', config)
            if snapshot.get('complete') is not True:
                fail('instrument_snapshots: every snapshot must explicitly declare complete=true')
            av = availability(snapshot)
            if cutoff and av and av > cutoff:
                diag['discarded_unavailable'].append({'snapshot': sd})
                continue
            if not av:
                diag['pit_missing_timestamps'].append(f'snapshot:{sd}')
            ids = []
            for item in snapshot.get('instruments', []):
                item = mapped(item, field_maps.get('catalog', {}))
                item['_snapshot_date'] = sd
                if av:
                    item.setdefault('available_at', av.isoformat())
                raw_catalog.append(item)
                ids.append(item.get('instrument_id'))
            if len(ids) != len(set(ids)):
                fail(f'instrument_snapshots/{sd}: duplicate instrument IDs')
            snapshots.append((sd, ids))
        snapshots.sort()
        if len({s[0] for s in snapshots}) != len(snapshots):
            fail('instrument_snapshots: duplicate source date')
    if not raw_catalog:
        fail('instruments: supply a catalog or dated complete instrument_snapshots')
    versions = defaultdict(list)
    for index, raw in enumerate(raw_catalog):
        if not isinstance(raw, dict):
            fail(f'instruments[{index}]: expected an object')
        item = mapped(raw, field_maps.get('catalog', {}))
        identity = item.get('instrument_id')
        if not isinstance(identity, str) or not identity:
            fail(f'instruments[{index}].instrument_id: required actual identifier')
        av = availability(item)
        if cutoff and av and av > cutoff:
            diag['discarded_unavailable'].append({'instrument_id': identity, 'metadata': True})
            continue
        if not av:
            diag['pit_missing_timestamps'].append(f'catalog:{identity}')
        for scope in ('exchange', 'family_id'):
            if item.get(scope, payload[scope]) != payload[scope]:
                fail(f'{identity}: cross-{scope} instrument unsupported')
        if item.get('kind') not in ('outright', 'calendar_spread'):
            fail(f'{identity}.kind: expected outright or calendar_spread')
        for key in ('listing_date', 'last_trading_date'):
            item[key] = day(item.get(key), f'{identity}.{key}', config)
        for key in ('expiry_date', 'first_notice_date', 'effective_from', 'effective_to'):
            if item.get(key):
                item[key] = day(item[key], f'{identity}.{key}', config)
        if item['listing_date'] > item['last_trading_date']:
            fail(f'{identity}: listing_date is after last_trading_date')
        if item.get('expiry_date', item['last_trading_date']) < item['listing_date']:
            fail(f'{identity}: expiry_date is before listing_date')
        if item.get('effective_to', '9999-12-31') < item.get('effective_from', '0001-01-01'):
            fail(f'{identity}: invalid effective date interval')
        item['expiry_order_field'] = 'expiry_date' if item.get('expiry_date') else 'last_trading_date'
        item['expiry_order_date'] = item[item['expiry_order_field']]
        item['product_id'] = item.get('product_id') or identity
        if not isinstance(item['product_id'], str):
            fail(f'{identity}.product_id: expected an actual string identifier')
        if item['kind'] == 'outright':
            item['delivery_month'] = month(item.get('delivery_month'), f'{identity}.delivery_month')
        else:
            if not isinstance(item.get('front_leg_id'), str) or not isinstance(item.get('back_leg_id'), str):
                fail(f'{identity}: explicit front_leg_id and back_leg_id are required; symbols are not parsed')
            if item.get('front_ratio', 1) != 1 or item.get('back_ratio', 1) != 1 or item.get('ratio', [1, 1]) != [1, 1]:
                fail(f'{identity}: only one-for-one two-leg spreads are supported')
            if 'legs' in item:
                fail(f'{identity}: use explicit front_leg_id/back_leg_id; ambiguous legs unsupported')
        for flag in item.get('eligibility', []):
            flag['date'] = day(flag.get('date'), f'{identity}.eligibility.date', config)
            for key in ('delivery_eligible', 'notice_eligible'):
                if key in flag and flag[key] is not None and not isinstance(flag[key], bool):
                    fail(f'{identity}.eligibility.{key}: expected boolean or null')
            availability(flag)
        versions[identity].append(item)
    for identity, items in versions.items():
        if len(items) > 1 and not snapshots:
            starts = [i.get('effective_from', '0001-01-01') for i in items]
            if len(starts) != len(set(starts)) and payload.get('revision_rule') != 'latest_available':
                fail(f'{identity}: duplicate catalog metadata; explicit latest_available revision_rule required')
            for start in set(starts):
                duplicates = [i for i in items if i.get('effective_from', '0001-01-01') == start]
                if len(duplicates) > 1 and (any(availability(i) is None for i in duplicates)
                                           or len({availability(i) for i in duplicates}) != len(duplicates)):
                    fail(f'{identity}: ambiguous catalog revision availability')
        kinds = {i['kind'] for i in items}
        if len(kinds) != 1:
            fail(f'{identity}: instrument kind changes across metadata versions')
        for item in items:
            if item['kind'] == 'calendar_spread':
                for key in ('front_leg_id', 'back_leg_id'):
                    leg = item[key]
                    if leg not in versions or versions[leg][0]['kind'] != 'outright':
                        fail(f'{identity}.{key}: missing cataloged outright {leg!r}')
                if item['front_leg_id'] == item['back_leg_id']:
                    fail(f'{identity}: spread legs must differ')
                for front in versions[item['front_leg_id']]:
                    for back in versions[item['back_leg_id']]:
                        if front['delivery_month'] >= back['delivery_month']:
                            fail(f'{identity}: front delivery month must precede back delivery month')
    grouped = defaultdict(list)
    for index, raw in enumerate(payload['daily']):
        if not isinstance(raw, dict):
            fail(f'daily[{index}]: expected an object')
        row = mapped(raw, field_maps.get('daily', {}))
        td = day(row.get('trade_date'), f'daily[{index}].trade_date', config)
        identity = row.get('instrument_id')
        if identity not in versions:
            # A catalog revision unavailable at the decision time cannot reveal an instrument.
            if any(x.get('instrument_id') == identity and x.get('metadata') for x in diag['discarded_unavailable']):
                continue
            fail(f'daily[{index}].instrument_id: missing catalog reference {identity!r}')
        if td not in sessions:
            fail(f'{identity}/{td}: trade_date absent from supplied sessions')
        for scope in ('exchange', 'family_id'):
            if row.get(scope, payload[scope]) != payload[scope]:
                fail(f'{identity}/{td}: conflicting {scope}')
        if 'volume' not in row:
            fail(f'{identity}/{td}: volume field required (use null for unknown)')
        row['trade_date'] = td
        unit = row.get('source_volume_unit', row.get('volume_unit', 'contracts'))
        factor = 1.0
        if unit != 'contracts':
            conversions = config.get('unit_conversions', {})
            if unit not in conversions:
                fail(f'{identity}/{td}: unknown volume unit {unit!r}; explicit conversion required')
            factor = number(conversions[unit], f'unit_conversions.{unit}')
            if factor <= 0:
                fail('unit conversion must be positive')
        for key in ('volume', 'open_interest', 'direct_outright_volume'):
            row[key] = number(row.get(key), f'{identity}/{td}.{key}', True, config)
            if row[key] is not None:
                row[key] *= factor
        if versions[identity][0]['kind'] == 'calendar_spread':
            if row.get('volume_basis', 'direct_spread') != 'direct_spread':
                fail(f'{identity}/{td}: individual spread volume must be direct_spread')
            if row['direct_outright_volume'] is not None:
                fail(f'{identity}/{td}: direct_outright_volume is not a spread quantity')
        elif row.get('volume_basis', payload['volume_basis']) != payload['volume_basis']:
            fail(f'{identity}/{td}: conflicting outright volume_basis')
        row['oi_date'] = day(row.get('oi_date', td), f'{identity}/{td}.oi_date', config)
        if row['oi_date'] > td:
            fail(f'{identity}/{td}: oi_date cannot follow trade_date')
        av = availability(row)
        if cutoff and av and av > cutoff and td <= policy['analysis_date']:
            diag['discarded_unavailable'].append({'instrument_id': identity, 'trade_date': td})
            continue
        if td <= policy['analysis_date'] and not av:
            diag['pit_missing_timestamps'].append(f'daily:{identity}:{td}')
        row['_conversion_factor'] = factor
        grouped[(td, identity)].append(row)
    declared_sources = {r.get('source', metadata.get('source')) for rows in grouped.values() for r in rows}
    declared_sources.discard(None)
    if len(declared_sources) > 1 or (metadata.get('source') and declared_sources and declared_sources != {metadata['source']}):
        fail('daily.source: conflicting sources in one batch; normalize upstream under an explicit reconciliation rule')
    observations = {}
    for key, rows in grouped.items():
        sources = {r.get('source', metadata.get('source')) for r in rows}
        if len(sources) > 1:
            fail(f'{key}: different sources cannot be silently merged')
        if len(rows) > 1:
            if payload.get('revision_rule') != 'latest_available':
                fail(f'{key}: duplicate observations; set explicit revision_rule=latest_available')
            if any(availability(r) is None for r in rows) or len({availability(r) for r in rows}) != len(rows):
                fail(f'{key}: revisions require distinct availability timestamps')
            rows.sort(key=availability)
            diag['resolved_revisions'].append({'trade_date': key[0], 'instrument_id': key[1],
                                               'versions': rows[:-1], 'selected': rows[-1]})
        observations[key] = rows[-1]
    for name in ('costs', 'resource_usage'):
        unknown = set(policy[name]) - set(versions)
        if unknown:
            fail(f'policy.{name}: unknown instrument IDs {sorted(unknown)}')
    return sessions, versions, observations, metadata, diag, snapshots


def catalog_on(versions, session, snapshots=None):
    result = {}
    snapshot_date = None
    if snapshots is not None:
        eligible = [s for s in snapshots if s[0] <= session]
        if not eligible:
            return {}, None
        snapshot_date, snapshot_ids = eligible[-1]
    for identity, items in versions.items():
        if snapshots is not None:
            candidates = [i for i in items if i.get('_snapshot_date') == snapshot_date and identity in snapshot_ids]
        else:
            candidates = [i for i in items if i.get('effective_from', '0001-01-01') <= session
                          <= i.get('effective_to', '9999-12-31')]
        if not candidates:
            continue
        item = max(candidates, key=lambda i: (i.get('effective_from', '0001-01-01'),
                                              availability(i) or datetime.min.replace(tzinfo=timezone.utc)))
        if item['listing_date'] <= session <= item['last_trading_date']:
            result[identity] = item
    return result, snapshot_date


def build_mapping(catalog, session):
    chain = sorted((i for i in catalog.values() if i['kind'] == 'outright'),
                   key=lambda i: (i['expiry_order_date'], i['delivery_month'], i['instrument_id']))
    ranks = {i['instrument_id']: n for n, i in enumerate(chain, 1)}
    rows = {}
    for item in sorted(catalog.values(), key=lambda i: i['instrument_id']):
        row = {k: item.get(k) for k in ('instrument_id', 'product_id', 'kind', 'delivery_month',
                                       'listing_date', 'last_trading_date', 'expiry_date',
                                       'expiry_order_date', 'expiry_order_field', 'first_notice_date')}
        row['days_to_expiry'] = days_between(item['expiry_order_date'], session)
        row['mapping_error'] = None
        if item['kind'] == 'outright':
            row.update(relative_label=f'C{ranks[item["instrument_id"]]}', rank=ranks[item['instrument_id']],
                       calendar_month_gap=month_number(item['delivery_month']) - month_number(chain[0]['delivery_month']))
        else:
            front, back = item['front_leg_id'], item['back_leg_id']
            row.update(front_leg_id=front, back_leg_id=back, relative_label=None,
                       calendar_month_gap=None)
            if front not in ranks or back not in ranks:
                row['mapping_error'] = 'One or both actual legs are not listed in this session chain'
            else:
                row['relative_label'] = f'S({ranks[front]},{ranks[back]})'
                row['front_delivery_month'] = catalog[front]['delivery_month']
                row['back_delivery_month'] = catalog[back]['delivery_month']
                row['calendar_month_gap'] = month_number(catalog[back]['delivery_month']) - month_number(catalog[front]['delivery_month'])
        rows[item['instrument_id']] = row
    return rows


def zero_allowed(metadata, session, identity, category):
    if metadata['reports_complete'] and metadata['absent_rows_mean_zero']:
        return True
    return any(d.get('complete') is True and d.get('absent_rows_mean_zero') is True
               and d.get('trade_date') == session
               and d.get('category', category) == category
               and (d.get('instrument_ids') is None or identity in d['instrument_ids'])
               for d in metadata.get('report_declarations', []))


def category_of(item):
    return 'outrights' if item['kind'] == 'outright' else 'spreads'


def metric(rows, requested_sessions=None, history_complete=True):
    known = [r['volume'] for r in rows if r['volume'] is not None]
    expected = len(rows)
    positive = sum(v > 0 for v in known)
    missing = expected - len(known)
    oi = [r for r in rows if r.get('open_interest') is not None]
    latest = max(oi, key=lambda r: (r['oi_date'], r['trade_date'])) if oi else None
    oi_unique = {(r.get('instrument_id'), r['oi_date']): r['open_interest'] for r in oi}
    result = {'total_known_volume': sum(known), 'adv': statistics.mean(known) if known else None,
              'median_volume': statistics.median(known) if known else None,
              'known_sessions': len(known), 'expected_sessions': expected,
              'missing_sessions': missing, 'positive_sessions': positive,
              'data_completeness': len(known) / expected if expected else None,
              'positive_frequency_lower': positive / expected if expected else None,
              'positive_frequency_upper': (positive + missing) / expected if expected else None,
              'positive_frequency_observed': positive / len(known) if known else None,
              'confirmed_inactive': bool(expected and not missing and not positive),
              'latest_oi': latest['open_interest'] if latest else None,
              'oi_date': latest['oi_date'] if latest else None,
              'average_oi': statistics.mean(oi_unique.values()) if oi_unique else None,
              'oi_observations': len(oi_unique)}
    if requested_sessions:
        result['rolling'] = {}
        for size in (20, 60):
            dates = set(requested_sessions[-size:])
            subset = [r for r in rows if r['trade_date'] in dates]
            child = metric(subset)
            complete = history_complete and len(dates) == size and len(subset) == size and child['missing_sessions'] == 0
            child['status'] = 'complete' if complete else 'partial'
            child['requested_sessions'] = size
            child['supplied_sessions'] = len(dates)
            result['rolling'][str(size)] = child
    return result


def category_metric(rows, sessions, catalog_complete=False, sessions_complete=False, rolling=False):
    """Category ADV is daily product volume, never mean volume per contract.

    A category day with an unknown constituent is incomplete. Its supplied
    known volume still contributes to the explicitly bounded coverage total,
    but that partial day cannot masquerade as a complete daily ADV sample.
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['trade_date']].append(row)
    daily = []
    partial_days = 0
    for session in sessions:
        observations = grouped[session]
        known = [r['volume'] for r in observations if r['volume'] is not None]
        complete = len(known) == len(observations) and (bool(observations) or catalog_complete)
        if known and not complete:
            partial_days += 1
        daily.append({'trade_date': session, 'volume': sum(known) if complete else None})
    result = metric(daily)
    result['total_complete_session_volume'] = result['total_known_volume']
    result['total_known_volume'] = sum(r['volume'] for r in rows if r['volume'] is not None)
    result['adv_basis'] = 'Complete supplied-category daily aggregates; partial dates excluded from ADV'
    result['partial_known_session_count'] = partial_days
    result['expected_instrument_sessions'] = len(rows)
    result['known_instrument_sessions'] = sum(r['volume'] is not None for r in rows)
    result['missing_instrument_sessions'] = sum(r['volume'] is None for r in rows)
    result['instrument_session_completeness'] = result['known_instrument_sessions'] / len(rows) if rows else None
    if rolling:
        result['rolling'] = {}
        for size in (20, 60):
            trailing = sessions[-size:]
            child = category_metric([r for r in rows if r['trade_date'] in set(trailing)], trailing,
                                    catalog_complete, sessions_complete)
            child['status'] = 'complete' if len(trailing) == size and sessions_complete and catalog_complete and not child['missing_sessions'] else 'partial'
            child['requested_sessions'] = size
            child['supplied_sessions'] = len(trailing)
            result['rolling'][str(size)] = child
    return result


def eligibility(item, session, policy, cutoff):
    flags = {}
    for key in ('delivery_eligible', 'notice_eligible'):
        if key in item:
            if item[key] is not None and not isinstance(item[key], bool):
                fail(f'{item["instrument_id"]}.{key}: expected boolean or null')
            flags[key] = item[key]
    for flag in sorted(item.get('eligibility', []), key=lambda f: f['date']):
        if flag['date'] <= session and (not cutoff or not availability(flag) or availability(flag) <= cutoff):
            flags.update({k: flag[k] for k in ('delivery_eligible', 'notice_eligible') if k in flag})
    if policy['exclude_on_or_after_first_notice']:
        flags['notice_eligible'] = session < item['first_notice_date'] if item.get('first_notice_date') else None
    if policy['min_days_to_last_trade'] is not None:
        flags['delivery_eligible'] = days_between(item['last_trading_date'], session) >= policy['min_days_to_last_trade']
    reasons, insufficient = [], False
    for flag, required in (('delivery_eligible', policy['require_delivery_eligible']),
                           ('notice_eligible', policy['require_notice_eligible'] or policy['exclude_on_or_after_first_notice'])):
        if flags.get(flag) is False:
            reasons.append(f'{flag}=false (prohibited)')
        elif required and flags.get(flag) is not True:
            reasons.append(f'{flag} is unknown')
            insufficient = True
    return flags, reasons, insufficient


def select(candidates, policy, denominators, has_policy):
    """Enumerate activity choices, closing over shared actual outright legs."""
    ids = sorted(i for i, c in candidates.items() if c['activity_eligible'])
    category = {i: category_of(c['instrument']) for i, c in candidates.items()}
    costs = {i: c['cost'] for i, c in candidates.items()}
    reasons = []
    weights = policy.get('weights', {'outrights': 1, 'spreads': 1})

    def evaluate(chosen):
        support = set(chosen)
        if policy['require_spread_legs']:
            for identity in chosen:
                item = candidates[identity]['instrument']
                if item['kind'] == 'calendar_spread':
                    support.update((item['front_leg_id'], item['back_leg_id']))
        if any(i not in candidates or not candidates[i]['dependency_eligible'] for i in support):
            return None
        category_cost = {k: sum(costs[i] for i in sorted(support) if category[i] == k)
                         for k in ('outrights', 'spreads')}
        total_cost = sum(category_cost.values())
        resource_cost = {r: sum(policy['resource_usage'].get(i, {}).get(r, 0) for i in sorted(support))
                         for r in policy['capacities']}
        if any(v > policy['capacities'][r] + 1e-10 for r, v in resource_cost.items()):
            return None
        if any(category_cost[k] > v + 1e-10 for k, v in policy.get('budgets', {}).items()):
            return None
        # Unspecified categories have zero budget unless a shared resource budget was given.
        if policy.get('mode') == 'budget' and 'shared_budget' not in policy:
            if any(category_cost[k] > 0 for k in category_cost if k not in policy.get('budgets', {})):
                return None
        if total_cost > policy.get('shared_budget', math.inf) + 1e-10:
            return None
        volumes = {k: sum(candidates[i]['metrics']['total_known_volume'] for i in sorted(support)
                          if category[i] == k) for k in ('outrights', 'spreads')}
        coverage = {k: (volumes[k] / d['total_known_volume'] if d['total_known_volume'] else 0.0)
                    if d['coverage_available'] else None for k, d in denominators.items()}
        score = sum(weights[k] * (coverage[k] or 0) for k in coverage)
        targets = policy.get('targets', {})
        met = all(coverage[k] is not None and coverage[k] + 1e-10 >= v for k, v in targets.items())
        deficits = {k: (max(0, v - coverage[k]) if coverage[k] is not None else None)
                    for k, v in targets.items()}
        return {'selected': sorted(chosen), 'support': sorted(support),
                'dependencies': sorted(support - set(chosen)), 'cost': total_cost,
                'category_cost': category_cost, 'resource_usage': resource_cost,
                'volumes': volumes, 'coverage': coverage, 'score': score,
                'targets_met': met, 'unmet_targets': {k: v for k, v in deficits.items() if v is None or v > 1e-10}}

    empty = evaluate(set())
    if not has_policy:
        return {'selection_status': 'policy_required', 'optimality': 'not_solved',
                'reason': 'Supply a budget or target policy to recommend support',
                'solution': empty, 'coverage_curves': [], 'evaluated_sets': 0}, evaluate

    def budget_key(s):
        return (-s['score'], s['cost'], len(s['support']), tuple(s['support']), tuple(s['selected']))

    def target_key(s):
        return (s['cost'], len(s['support']), -s['score'], tuple(s['support']), tuple(s['selected']))

    def partial_key(s):
        deficits = s['unmet_targets']
        return (sum(1 if v is None else v for v in deficits.values()), *budget_key(s))

    # With independent equal-cost items within each category, the best set at
    # each count is a volume-sorted prefix. Enumerating category prefix pairs
    # is exact and avoids exponential search for long outright chains.
    independent = not policy['require_spread_legs'] and not policy['capacities']
    grouped_ids = {k: sorted((i for i in ids if category[i] == k),
                            key=lambda i: (-candidates[i]['metrics']['total_known_volume'], i))
                   for k in ('outrights', 'spreads')}
    independent &= all(len({costs[i] for i in group}) <= 1 for group in grouped_ids.values())
    exact = independent or len(ids) <= policy['exact_search_limit']
    if not exact and not policy['allow_heuristic']:
        return {'selection_status': 'not_solved', 'optimality': 'not_solved',
                'reason': f'{len(ids)} candidates exceed exact_search_limit={policy["exact_search_limit"]}; '
                          'raise the limit (maximum 24) or explicitly enable allow_heuristic',
                'solution': empty, 'coverage_curves': [], 'evaluated_sets': 0}, evaluate
    frontier = {}
    best_met = None
    best_partial = empty
    count = 0

    def consider(solution):
        nonlocal best_met, best_partial, count
        count += 1
        if solution is None:
            return
        n = len(solution['support'])
        if n not in frontier or budget_key(solution) < budget_key(frontier[n]):
            frontier[n] = solution
        if partial_key(solution) < partial_key(best_partial):
            best_partial = solution
        valid = solution['targets_met'] if policy['mode'] == 'target' else True
        key = target_key if policy['mode'] == 'target' else budget_key
        if valid and (best_met is None or key(solution) < key(best_met)):
            best_met = solution

    if independent:
        reasons.append('Exact volume-sorted category prefixes with equal per-category costs and independent instruments')
        outs, spreads = grouped_ids['outrights'], grouped_ids['spreads']
        for n in range(len(outs) + 1):
            for m in range(len(spreads) + 1):
                consider(evaluate(set(outs[:n]) | set(spreads[:m])))
    elif exact:
        for size in range(len(ids) + 1):
            for subset in itertools.combinations(ids, size):
                consider(evaluate(set(subset)))
    else:
        reasons.append('Greedy marginal-score-per-incremental-cost search; coupled constraints may be suboptimal')
        chosen = set()
        consider(empty)
        remaining = set(ids)
        current = empty
        while remaining:
            proposals = []
            for identity in sorted(remaining):
                proposal = evaluate(chosen | {identity})
                if proposal is not None:
                    gain = proposal['score'] - current['score']
                    extra = proposal['cost'] - current['cost']
                    proposals.append((-(gain / extra if extra else gain * 1e12), budget_key(proposal), identity, proposal))
            if not proposals:
                break
            _, _, identity, current = min(proposals)
            chosen.add(identity)
            remaining.remove(identity)
            consider(current)
    solution = best_met or best_partial
    status = 'selected' if best_met else ('infeasible' if exact else 'target_not_met')
    relevant = set(policy.get('targets', {})) if policy['mode'] == 'target' else (
        {k for k, v in policy['budgets'].items() if v > 0} if 'shared_budget' not in policy
        else {k for k, v in weights.items() if v > 0})
    unknown = [k for k, d in denominators.items() if k in relevant and not d['coverage_available']]
    if unknown:
        reasons.append('Unknown volume basis or absent activity observations prevent coverage claims for: ' + ', '.join(unknown))
        reasons.append('Any returned support set optimizes only measurable category contributions; the full requested objective is unresolved')
        status = 'insufficient_data'
    return {'selection_status': status, 'optimality': 'not_solved' if unknown else ('proven' if exact else 'heuristic'),
            'reason': '; '.join(reasons) or 'Exhaustive deterministic search of feasible activity subsets',
            'scoring_formula': 'sum(category_weight * known category volume coverage); no combined volume denominator',
            'tie_break': 'minimum cost, minimum support count, sorted actual IDs',
            'objective': 'minimum total support cost then instrument count' if policy['mode'] == 'target'
                         else 'maximum weighted sum of separate category known-volume coverage',
            'score_upper_bound': None if unknown else (solution['score'] if exact and policy['mode'] == 'budget' else sum(weights[k] for k in weights if denominators[k]['coverage_available'])),
            'solution': solution, 'evaluated_sets': count,
            'coverage_curves': [{'instrument_count': n, 'cost': s['cost'], 'coverage': s['coverage'],
                                 'support': s['support'], 'score': s['score'],
                                 'optimality': 'not_solved' if unknown else ('proven' if exact else 'heuristic')}
                                for n, s in sorted(frontier.items())]}, evaluate


def analyze(payload: dict) -> dict:
    """Analyze one exchange/family payload without file writes or network access."""
    if not isinstance(payload, dict):
        fail('payload: expected an object')
    payload = copy.deepcopy(payload)
    policy, has_policy = policy_for(payload)
    sessions, versions, observations, metadata, diag, snapshots = normalize(payload, policy)
    analysis_date = policy['analysis_date']
    cutoff = instant(policy['decision_time'], 'decision_time') if policy['decision_time'] else None
    history = [s for s in sessions if s <= analysis_date]
    if policy['start_date']:
        window = [s for s in history if s >= policy['start_date']]
    else:
        window = history[-policy['lookback_sessions']:]
    window_set = set(window)
    window_rows, date_mappings, expected_keys = [], [], set()
    per_instrument = defaultdict(list)
    all_per_instrument = defaultdict(list)
    relative, all_relative = defaultdict(list), defaultdict(list)
    month_groups, dte_groups = defaultdict(list), defaultdict(list)
    current_catalog, current_mapping = {}, {}
    for session in history:
        catalog, snapshot_date = catalog_on(versions, session, snapshots)
        mapping = build_mapping(catalog, session)
        for identity, item in catalog.items():
            flags, restriction_reasons, _ = eligibility(item, session, policy, cutoff)
            mapping[identity]['eligibility'] = flags
            mapping[identity]['eligibility_reasons'] = restriction_reasons
        outright_rows = []
        for identity, item in sorted(catalog.items()):
            category = category_of(item)
            supplied = observations.get((session, identity))
            obs = dict(supplied) if supplied is not None else {
                'trade_date': session, 'instrument_id': identity, 'volume': None,
                'open_interest': None, 'oi_date': session, 'direct_outright_volume': None}
            if supplied is None:
                if zero_allowed(metadata, session, identity, category):
                    obs['volume'] = 0.0
                    obs['imputed_zero'] = True
                    diag['zero_imputations'].append({'trade_date': session, 'instrument_id': identity,
                                                      'reason': 'Explicit complete report and omission-means-zero declaration'})
                else:
                    diag['missing_rows'].append({'trade_date': session, 'instrument_id': identity})
            row = {**obs, 'kind': item['kind'], 'category': category,
                   'relative_label': mapping[identity]['relative_label'],
                   'days_to_expiry': mapping[identity]['days_to_expiry'],
                   'delivery_month': item.get('delivery_month'), 'product_id': item['product_id']}
            expected_keys.add((session, identity))
            all_per_instrument[identity].append(row)
            relative_label = mapping[identity]['relative_label']
            # Actual spread IDs remain separate even if vendor product codes and labels coincide.
            relative_key = (category, relative_label, identity if category == 'spreads' else None)
            if relative_label:
                all_relative[relative_key].append(row)
            if session in window_set:
                window_rows.append(row)
                per_instrument[identity].append(row)
                label = mapping[identity]['relative_label']
                if label:
                    relative[relative_key].append(row)
                if item.get('delivery_month'):
                    month_groups[(category, item['delivery_month'])].append(row)
                dte = row['days_to_expiry']
                band = 'past_expiry' if dte < 0 else '0-7' if dte <= 7 else '8-30' if dte <= 30 else '31-90' if dte <= 90 else '91+'
                dte_groups[(category, band)].append(row)
            if item['kind'] == 'outright':
                outright_rows.append(row)
            if mapping[identity]['mapping_error']:
                diag['mapping_errors'].append({'trade_date': session, 'instrument_id': identity,
                                                'error': mapping[identity]['mapping_error']})
        known = [r for r in outright_rows if r['volume'] is not None]
        maximum = max((r['volume'] for r in known), default=None)
        complete = bool(outright_rows and len(known) == len(outright_rows) and metadata['catalog_complete']
                        and (snapshots is None or snapshot_date == session))
        chain = sorted((m for m in mapping.values() if m['kind'] == 'outright'), key=lambda m: m['rank'])
        date_mappings.append({'trade_date': session, 'snapshot_source_date': snapshot_date,
                              'outrights': chain,
                              'spreads': [m for m in mapping.values() if m['kind'] == 'calendar_spread'],
                              'nearest_expiry': [m['instrument_id'] for m in chain
                                                 if chain and m['expiry_order_date'] == chain[0]['expiry_order_date']],
                              'most_active': {'instrument_ids': sorted(r['instrument_id'] for r in known if r['volume'] == maximum),
                                              'volume': maximum, 'ties': sum(r['volume'] == maximum for r in known) > 1,
                                              'data_complete': complete,
                                              'scope': 'complete_market' if complete else 'supplied_known_observations'}})
        if session == analysis_date:
            current_catalog, current_mapping = catalog, mapping
    for (td, identity), obs in observations.items():
        if td <= analysis_date and (td, identity) not in expected_keys:
            fail(f'{identity}/{td}: observation outside listed lifecycle or effective catalog/snapshot')
    denominators = {}
    catalog_history_complete = metadata['catalog_complete'] and (snapshots is None or all(s in {d for d, _ in snapshots} for s in window))
    for category in ('outrights', 'spreads'):
        rows = [r for r in window_rows if r['category'] == category]
        m = category_metric(rows, window, catalog_history_complete, metadata['sessions_complete'])
        historical_rows = [r for records in all_per_instrument.values() for r in records if r['category'] == category]
        m['rolling'] = category_metric(historical_rows, history, catalog_history_complete,
                                       metadata['sessions_complete'], rolling=True)['rolling']
        denominators[category] = {**m, 'volume_basis': payload['volume_basis'] if category == 'outrights' else 'direct_spread',
                                  'volume_unit': 'contracts' if category == 'outrights' else 'spread_contracts',
                                  'definition_known': category == 'spreads' or payload['volume_basis'] != 'unknown',
                                  'coverage_available': (category == 'spreads' or payload['volume_basis'] != 'unknown')
                                                        and (m['known_instrument_sessions'] > 0 or (not m['expected_instrument_sessions'] and catalog_history_complete)),
                                  'scope': 'complete_supplied_market' if catalog_history_complete and metadata['sessions_complete']
                                           and m['missing_sessions'] == 0 else 'supplied_observed_universe',
                                  'coverage_basis': 'known volume only; unknown activity is excluded from denominator',
                                  'expected_instrument_sessions': m['expected_instrument_sessions']}
        denominators[category]['open_interest_by_instrument'] = [
            {'instrument_id': i, **{k: metric(records)[k] for k in ('latest_oi', 'oi_date', 'average_oi', 'oi_observations')}}
            for i, records in sorted(per_instrument.items()) if records and records[0]['category'] == category
            and any(r.get('open_interest') is not None for r in records)]
    instrument_metrics = {}
    for identity in sorted(versions):
        rows = per_instrument[identity]
        m = metric(rows)
        # Rolling metrics use the complete supplied trailing history, independent of a custom start date.
        m['rolling'] = metric(all_per_instrument[identity], history, metadata['sessions_complete'])['rolling']
        historical_oi = [r for r in all_per_instrument[identity] if r.get('open_interest') is not None]
        latest = max(historical_oi, key=lambda r: (r['oi_date'], r['trade_date'])) if historical_oi else None
        m.update(latest_oi=latest['open_interest'] if latest else None,
                 oi_date=latest['oi_date'] if latest else None,
                 oi_age_days=days_between(analysis_date, latest['oi_date']) if latest else None)
        category = category_of(versions[identity][0])
        denom = denominators[category]
        m['volume_share'] = m['total_known_volume'] / denom['total_known_volume'] if denom['coverage_available'] and denom['total_known_volume'] else (0.0 if denom['coverage_available'] else None)
        m['actual_instrument_id'] = identity
        instrument_metrics[identity] = m
    candidates = {}
    for identity, item in sorted(current_catalog.items()):
        m = instrument_metrics[identity]
        flags, restrictions, insuff = eligibility(item, analysis_date, policy, cutoff)
        reasons = list(restrictions)
        if current_mapping[identity]['mapping_error']:
            reasons.append(current_mapping[identity]['mapping_error'])
            insuff = True
        if not m['expected_sessions']:
            reasons.append('No expected observations in requested period')
            insuff = True
        if m['data_completeness'] is None or m['data_completeness'] < policy['min_data_completeness']:
            reasons.append('Data completeness below required minimum')
            insuff = True
        if policy['min_oi'] is not None:
            if m['latest_oi'] is None:
                reasons.append('Required open interest unavailable')
                insuff = True
            elif m['latest_oi'] < policy['min_oi']:
                reasons.append('Open interest below required minimum')
        if policy['max_oi_age_days'] is not None:
            if m['oi_age_days'] is None or m['oi_age_days'] > policy['max_oi_age_days']:
                reasons.append('Required fresh open interest unavailable or stale')
                insuff = True
        dep_eligible = not reasons
        activity_reasons = []
        unknown_definition = item['kind'] == 'outright' and payload['volume_basis'] == 'unknown'
        if unknown_definition:
            activity_reasons.append('Outright volume definition unknown; coverage-based activity selection unavailable')
            insuff = True
        if not m['total_known_volume']:
            activity_reasons.append('No confirmed positive activity')
        if m['adv'] is None or m['adv'] < policy['min_adv']:
            activity_reasons.append('Observed ADV below required minimum')
        if m['positive_frequency_lower'] is None or m['positive_frequency_lower'] < policy['min_positive_frequency']:
            activity_reasons.append('Conservative positive-session frequency below required minimum')
        conditional = []
        for rule in policy['conditional_rules']:
            if not isinstance(rule.get('name'), str) or not isinstance(rule.get('delivery_months'), list):
                fail('conditional_rules: each rule needs name and delivery_months (month numbers)')
            if item.get('delivery_month') and int(item['delivery_month'][5:]) in rule['delivery_months']:
                minimum = number(rule.get('min_known_sessions', 20), 'conditional_rule.min_known_sessions')
                years = {r['trade_date'][:4] for r in per_instrument[identity] if r['volume'] is not None}
                if m['known_sessions'] >= minimum and len(years) >= rule.get('min_years', 1) and dep_eligible and not activity_reasons:
                    conditional.append({'rule': rule['name'], 'known_sessions': m['known_sessions'], 'observed_years': sorted(years)})
        candidates[identity] = {'instrument': item, 'mapping': current_mapping[identity], 'metrics': m,
                                'cost': policy['costs'].get(identity, 1.0), 'eligibility': flags,
                                'dependency_eligible': dep_eligible, 'activity_eligible': dep_eligible and not activity_reasons,
                                'insufficient': insuff, 'reasons': reasons + activity_reasons,
                                'conditional_evidence': conditional}
    if policy['require_spread_legs']:
        for identity, c in candidates.items():
            item = c['instrument']
            if item['kind'] == 'calendar_spread':
                bad = [leg for leg in (item['front_leg_id'], item['back_leg_id'])
                       if leg not in candidates or not candidates[leg]['dependency_eligible']]
                if bad:
                    c['activity_eligible'] = False
                    c['insufficient'] |= any(leg not in candidates or candidates[leg]['insufficient'] for leg in bad)
                    c['reasons'].append('Required outright legs unavailable/prohibited: ' + ', '.join(bad))
    selection, evaluate = select(candidates, policy, denominators, has_policy)
    solution = selection['solution']
    packages, introduced = [], set()
    previous = evaluate(set())
    for identity in solution['selected']:
        introduced.add(identity)
        step = evaluate(introduced)
        packages.append({'activity_instrument_id': identity,
                         'added_support': sorted(set(step['support']) - set(previous['support'])),
                         'incremental_cost': step['cost'] - previous['cost'],
                         'marginal_category_coverage': {
                             k: step['coverage'][k] - previous['coverage'][k]
                             if step['coverage'][k] is not None and previous['coverage'][k] is not None else None
                             for k in denominators}})
        previous = step
    selection['marginal_support_packages'] = packages
    selection['marginal_order'] = 'Sorted activity instrument IDs; shared dependencies charged at first introduction'
    selected, dependencies = set(solution['selected']), set(solution['dependencies'])
    recommendations = []
    for identity, c in candidates.items():
        m = c['metrics']
        reasons = list(c['reasons'])
        if c['insufficient']:
            label, action = 'insufficient_data', 'defer'
        elif not c['dependency_eligible']:
            label, action = 'low_priority', 'ineligible'
        elif m['confirmed_inactive']:
            label, action = 'confirmed_inactive', 'do_not_support'
        else:
            label, action = 'low_priority', 'do_not_support' if has_policy else 'defer'
        if identity in selected:
            label = 'seasonal/conditional' if c['conditional_evidence'] else 'core'
            action = 'support'
            reasons = ['Selected for observed activity under supplied support constraints']
        elif identity in dependencies:
            action = 'dependency_only'
            reasons = ['Required actual outright leg of selected spread(s)']
        elif c['activity_eligible']:
            reasons.append('Policy required' if not has_policy else 'Not selected under available capacity/target policy')
            if selection['selection_status'] in ('not_solved', 'insufficient_data'):
                action = 'defer'
                reasons[-1] = 'Selection not solved; no capacity-based exclusion established'
        if m['missing_sessions'] and identity not in solution['support'] and action != 'ineligible':
            label, action = 'insufficient_data', 'defer'
            reasons.append('Missing activity prevents a confident exclusion or confirmed inactivity')
        denom = denominators[category_of(c['instrument'])]
        marginal = m['total_known_volume'] / denom['total_known_volume'] if denom['definition_known'] and denom['total_known_volume'] else None
        recommendations.append({'instrument_id': identity, 'product_id': c['instrument']['product_id'],
                                'kind': c['instrument']['kind'], 'relative_label': c['mapping']['relative_label'],
                                'label': label, 'action': action, 'reasons': reasons,
                                'cost': c['cost'], 'metrics': m, 'eligibility': c['eligibility'],
                                'activity_eligible': c['activity_eligible'],
                                'data_confidence': 'complete_observations' if m['data_completeness'] == 1 else 'incomplete_observations',
                                'conditional_evidence': c['conditional_evidence'],
                                'marginal_category_coverage': marginal if identity in solution['support'] else 0.0})
    recommendations.sort(key=lambda r: (0 if r['kind'] == 'outright' else 1, -r['metrics']['total_known_volume'], r['instrument_id']))
    # Prefix comparison uses exactly the same candidate restrictions and configured resource costs.
    current_chain = sorted((c for c in candidates.values() if c['instrument']['kind'] == 'outright'), key=lambda c: c['mapping']['rank'])
    baseline = []
    prefix = set()
    for c in current_chain:
        prefix.add(c['instrument']['instrument_id'])
        eligible = all(candidates[i]['activity_eligible'] for i in prefix)
        result = evaluate(prefix) if eligible else None
        baseline.append({'through': c['mapping']['relative_label'], 'support': sorted(prefix),
                         'feasible': result is not None,
                         'reason': None if result else 'Prefix violates activity/eligibility/cost constraints',
                         'cost': result['cost'] if result else sum(candidates[i]['cost'] for i in prefix),
                         'coverage': result['coverage'] if result else None})
    relative_metrics = []
    for key, rows in sorted(relative.items(), key=lambda pair: tuple(str(v) for v in pair[0])):
        category, label, identity = key
        m = metric(rows)
        m['rolling'] = metric(all_relative[key], history, metadata['sessions_complete'])['rolling']
        denom = denominators[category]
        m['volume_share'] = m['total_known_volume'] / denom['total_known_volume'] if denom['definition_known'] and denom['total_known_volume'] else None
        relative_metrics.append({'category': category, 'relative_label': label,
                                 'instrument_id': identity,
                                 'product_id': versions[identity][0]['product_id'] if identity else None,
                                 'actual_instrument_ids': sorted({r['instrument_id'] for r in rows}), 'metrics': m,
                                 'sample_unit': 'instrument_sessions'})
    forward = []
    for session in sessions:
        if session <= analysis_date:
            continue
        catalog, _ = catalog_on(versions, session, snapshots)
        result = {'trade_date': session, 'categories': {}}
        for category in ('outrights', 'spreads'):
            listed = [i for i, item in catalog.items() if category_of(item) == category]
            known = {i: observations[(session, i)]['volume'] for i in listed if (session, i) in observations
                     and observations[(session, i)]['volume'] is not None}
            for identity in listed:
                if (session, identity) not in observations and zero_allowed(metadata, session, identity, category):
                    known[identity] = 0.0
            total = sum(known.values())
            captured = sum(v for i, v in known.items() if i in solution['support'])
            result['categories'][category] = {'total_known_volume': total, 'supported_known_volume': captured,
                                              'coverage': captured / total if total and denominators[category]['definition_known'] else None,
                                              'known_instrument_count': len(known), 'expected_instrument_count': len(listed),
                                              'missing_instrument_count': len(listed) - len(known)}
        forward.append(result)
    pit_verified = bool(cutoff and not diag['pit_missing_timestamps'])
    flags_without_time = [i for i, items in versions.items() for item in items
                          if item.get('eligibility') and any(not availability(f) for f in item['eligibility'])]
    if flags_without_time:
        pit_verified = False
        diag['pit_missing_timestamps'].extend('eligibility:' + i for i in flags_without_time)
    direct_rows = [{**r, 'volume': r.get('direct_outright_volume')} for r in window_rows if r['category'] == 'outrights']
    report = {
        'schema_version': '1.0', 'exchange': payload['exchange'], 'family_id': payload['family_id'],
        'analysis_date': analysis_date, 'period': {'start': window[0], 'end': window[-1], 'supplied_trading_days': len(window),
                                                'requested_lookback_sessions': None if policy['start_date'] else policy['lookback_sessions'],
                                                'history_complete': metadata['sessions_complete']},
        'effective_policy': policy, 'source_metadata': metadata,
        'point_in_time': {'status': 'verified_against_supplied_availability' if pit_verified else 'unverified',
                          'limitation': None if pit_verified else 'Decision cutoff and publication/availability timestamps are required for observations, metadata and eligibility; supplied timestamps are trusted'},
        'accounting': 'Outright volume and direct spread lots use separate denominators; they are never summed as market coverage',
        'category_denominators': denominators, 'date_mappings': date_mappings,
        'instrument_metrics': instrument_metrics, 'relative_metrics': relative_metrics,
        'additional_direct_outright_metrics': metric(direct_rows),
        'activity_by_delivery_month': [{'category': k[0], 'delivery_month': k[1], 'metrics': metric(rows)} for k, rows in sorted(month_groups.items())],
        'activity_by_days_to_expiry': [{'category': k[0], 'band': k[1], 'metrics': metric(rows)} for k, rows in sorted(dte_groups.items())],
        'seasonality': {'conclusion': 'No seasonal inference; conditional labels require explicit rules and recorded sample evidence'},
        'selection_status': selection['selection_status'], 'optimality': selection['optimality'], 'selection': selection,
        'limitations': ([ 'Supplied session calendar is not declared complete; missing exchange sessions cannot be identified or generated' ]
                        if not metadata['sessions_complete'] else [])
                       + ([ 'Catalog coverage is not complete across the decision period; market coverage is limited to supplied instruments' ]
                          if not catalog_history_complete else []),
        'selected_instruments': solution['selected'], 'supporting_legs': solution['dependencies'],
        'supported_universe_mappings': [{'trade_date': d['trade_date'], 'instruments': [m for m in d['outrights'] + d['spreads']
                                                                                      if m['instrument_id'] in solution['support']]} for d in date_mappings],
        'recommendations': recommendations,
        'excluded_candidates': [r for r in recommendations if r['action'] not in ('support', 'dependency_only')],
        'contiguous_chain_comparison': baseline, 'diagnostics': diag,
        'provenance': {'catalog_versions': dict(versions), 'observations': [observations[k] for k in sorted(observations)],
                       'snapshot_dates': [s[0] for s in snapshots] if snapshots is not None else None},
        'forward_validation': forward,
    }
    return report


def synthetic_example():
    """Five supplied trading days, four outrights, two actual spread products.

    Budget 4 selects A, D, AB and includes B solely as AB's supporting leg.
    C is active but loses under capacity. Five days means ADV20/60 are partial.
    """
    sessions = ['2025-03-24', '2025-03-25', '2025-03-26', '2025-03-27', '2025-03-28']
    catalog = []
    for identity, delivery, ltd in [('A', '2025-06', '2025-06-20'),
                                     ('B', '2025-09', '2025-09-19'),
                                     ('C', '2025-12', '2025-12-19'),
                                     ('D', '2026-03', '2026-03-20')]:
        catalog.append({'instrument_id': identity, 'kind': 'outright', 'delivery_month': delivery,
                        'listing_date': '2024-01-01', 'last_trading_date': ltd})
    for identity, front, back in [('AB', 'A', 'B'), ('BC', 'B', 'C')]:
        catalog.append({'instrument_id': identity, 'product_id': 'EXAMPLE-' + identity,
                        'kind': 'calendar_spread', 'front_leg_id': front, 'back_leg_id': back,
                        'listing_date': '2024-01-01', 'last_trading_date': '2025-06-20'})
    daily = [{'trade_date': td, 'instrument_id': identity, 'volume': value,
              'open_interest': 1000 if identity == 'A' else None,
              'available_at': td + 'T23:00:00Z'}
             for td in sessions for identity, value in [('A', 100), ('B', 0), ('C', 40), ('D', 180), ('AB', 80), ('BC', 10)]]
    for item in catalog:
        item['available_at'] = '2024-01-01T00:00:00Z'
    return {'example_notice': 'SYNTHETIC DATA ONLY; no real exchange identifiers or market claims',
            'exchange': 'EXAMPLE', 'family_id': 'TEST', 'sessions': sessions,
            'volume_basis': 'contract_month_total', 'volume_unit': 'contracts',
            'metadata': {'source': 'synthetic_example', 'catalog_complete': True,
                         'sessions_complete': True, 'reports_complete': True,
                         'absent_rows_mean_zero': False},
            'instruments': catalog, 'daily': daily,
            'policy': {'mode': 'budget', 'analysis_date': sessions[-1],
                       'decision_time': '2025-03-29T00:00:00Z', 'start_date': sessions[0],
                       'shared_budget': 4, 'weights': {'outrights': 1, 'spreads': 1},
                       'require_spread_legs': True}}


def export_csv(report, filename):
    fields = ['instrument_id', 'product_id', 'kind', 'relative_label', 'label', 'action',
              'cost', 'total_known_volume', 'adv', 'median_volume', 'known_sessions',
              'expected_sessions', 'missing_sessions', 'data_completeness', 'volume_share',
              'positive_frequency_lower', 'positive_frequency_upper', 'latest_oi', 'oi_date',
              'oi_age_days', 'marginal_category_coverage', 'reason', 'data_confidence']
    with open(filename, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for rec in report['recommendations']:
            row = {**rec, **rec['metrics'], 'reason': '; '.join(rec['reasons'])}
            writer.writerow(row)


def summary(report):
    solution = report['selection']['solution']
    lines = [f'{report["exchange"]}/{report["family_id"]}: {report["period"]["start"]} through '
             f'{report["analysis_date"]} ({report["period"]["supplied_trading_days"]} trading days)',
             f'Selection: {report["selection_status"]}; optimality: {report["optimality"]}; cost: {solution["cost"]:g}',
             'Actual ID  Relative   ADV        Label                 Action']
    for r in report['recommendations']:
        adv = f'{r["metrics"]["adv"]:.2f}' if r['metrics']['adv'] is not None else 'unknown'
        lines.append(f'{r["instrument_id"]:<10} {str(r["relative_label"]):<10} {adv:<10} {r["label"]:<21} {r["action"]}')
    for category, coverage in solution['coverage'].items():
        text = f'{coverage:.1%}' if coverage is not None else 'unknown'
        lines.append(f'{category}: {text} of supplied known volume ({report["category_denominators"][category]["volume_basis"]})')
    return '\n'.join(lines)


class CoverageTests(unittest.TestCase):
    def base(self):
        return synthetic_example()

    def rec(self, report, identity):
        return next(r for r in report['recommendations'] if r['instrument_id'] == identity)

    def test_end_to_end_shared_budget_dependencies(self):
        r = analyze(self.base())
        self.assertEqual(r['selected_instruments'], ['A', 'AB', 'D'])
        self.assertEqual(r['supporting_legs'], ['B'])
        self.assertEqual(r['selection']['solution']['cost'], 4)
        self.assertEqual(self.rec(r, 'B')['action'], 'dependency_only')
        self.assertEqual(self.rec(r, 'B')['label'], 'confirmed_inactive')
        self.assertEqual(self.rec(r, 'C')['label'], 'low_priority')
        self.assertEqual(r['optimality'], 'proven')
        json.dumps(r, allow_nan=False)

    def test_expiry_roll_nonmonthly_and_order(self):
        p = self.base()
        p['instruments'][0]['last_trading_date'] = '2025-03-25'
        p['instruments'][1]['expiry_date'] = '2025-03-24'
        p['daily'] = [r for r in p['daily'] if r['instrument_id'] != 'A' or r['trade_date'] <= '2025-03-25']
        r = analyze(p)
        self.assertEqual(r['date_mappings'][0]['outrights'][0]['instrument_id'], 'B')
        self.assertEqual(r['date_mappings'][0]['outrights'][0]['expiry_order_field'], 'expiry_date')
        self.assertEqual(r['date_mappings'][1]['outrights'][1]['instrument_id'], 'A')
        self.assertNotIn('A', [m['instrument_id'] for m in r['date_mappings'][2]['outrights']])
        self.assertTrue(r['date_mappings'][2]['spreads'][0]['mapping_error'])

    def test_zero_unknown_ranks_and_missing(self):
        p = self.base()
        p['daily'] = [r for r in p['daily'] if not (r['instrument_id'] == 'A' and r['trade_date'] == p['sessions'][0])]
        next(r for r in p['daily'] if r['instrument_id'] == 'C')['volume'] = None
        r = analyze(p)
        self.assertEqual([m['instrument_id'] for m in r['date_mappings'][0]['outrights']], ['A', 'B', 'C', 'D'])
        self.assertEqual(self.rec(r, 'A')['label'], 'insufficient_data')
        self.assertFalse(r['instrument_metrics']['A']['confirmed_inactive'])
        self.assertEqual(r['instrument_metrics']['A']['missing_sessions'], 1)
        self.assertFalse(r['date_mappings'][0]['most_active']['data_complete'])

    def test_spread_relative_changes_distinct_products(self):
        p = self.base()
        p['instruments'][0]['last_trading_date'] = '2025-03-25'
        p['daily'] = [r for r in p['daily'] if r['instrument_id'] != 'A' or r['trade_date'] <= '2025-03-25']
        second = dict(p['instruments'][-1], instrument_id='BC2', product_id='BC-OTHER')
        p['instruments'].append(second)
        p['daily'] += [dict(r, instrument_id='BC2') for r in p['daily'] if r['instrument_id'] == 'BC']
        r = analyze(p)
        self.assertEqual(next(m for m in r['date_mappings'][0]['spreads'] if m['instrument_id'] == 'BC')['relative_label'], 'S(2,3)')
        self.assertEqual(next(m for m in r['date_mappings'][-1]['spreads'] if m['instrument_id'] == 'BC')['relative_label'], 'S(1,2)')
        self.assertEqual(len([m for m in r['relative_metrics'] if m['category'] == 'spreads' and m['relative_label'] == 'S(1,2)']), 3)

    def test_accounting_no_inference(self):
        p = self.base()
        for row in p['daily']:
            if row['instrument_id'] in ('AB', 'BC'):
                row['volume'] = None
        r = analyze(p)
        self.assertEqual(r['category_denominators']['outrights']['total_known_volume'], 1600)
        self.assertIsNone(r['additional_direct_outright_metrics']['adv'])
        self.assertEqual(r['category_denominators']['spreads']['total_known_volume'], 0)
        self.assertEqual(self.rec(r, 'AB')['label'], 'insufficient_data')
        self.assertIsNone(r['instrument_metrics']['D']['latest_oi'])

    def test_frequency_and_rolling_partial_zero_convention(self):
        p = self.base()
        p['daily'] = [row for row in p['daily'] if not (row['instrument_id'] == 'A' and row['trade_date'] == p['sessions'][0])]
        r = analyze(p)
        m = r['instrument_metrics']['A']
        self.assertEqual(m['positive_frequency_lower'], .8)
        self.assertEqual(m['positive_frequency_upper'], 1)
        self.assertEqual(m['rolling']['20']['status'], 'partial')
        p['metadata']['absent_rows_mean_zero'] = True
        r = analyze(p)
        self.assertEqual(r['instrument_metrics']['A']['adv'], 80)
        self.assertEqual(len(r['diagnostics']['zero_imputations']), 1)
        p['metadata']['reports_complete'] = False
        self.assertEqual(analyze(p)['instrument_metrics']['A']['missing_sessions'], 1)

    def test_full_rolling_and_listing_history(self):
        p = self.base()
        # Deliberately use a supplied sparse calendar, never generated weekdays.
        p['sessions'] = [date.fromordinal(date(2025, 1, 1).toordinal() + n * 2).isoformat() for n in range(60)]
        p['daily'] = [{'trade_date': d, 'instrument_id': i['instrument_id'], 'volume': 2}
                      for d in p['sessions'] for i in p['instruments']]
        p['policy'].pop('decision_time')
        p['policy']['analysis_date'] = p['sessions'][-1]
        p['policy']['start_date'] = p['sessions'][0]
        r = analyze(p)
        self.assertEqual(r['instrument_metrics']['A']['rolling']['60']['status'], 'complete')
        self.assertEqual(r['instrument_metrics']['A']['rolling']['60']['adv'], 2)
        p['instruments'][0]['listing_date'] = p['sessions'][-10]
        p['daily'] = [row for row in p['daily'] if row['instrument_id'] != 'A' or row['trade_date'] >= p['sessions'][-10]]
        r = analyze(p)
        self.assertEqual(r['instrument_metrics']['A']['expected_sessions'], 10)
        self.assertEqual(r['instrument_metrics']['A']['rolling']['20']['status'], 'partial')

    def test_variable_cost_exact_against_independent_enumeration(self):
        p = self.base()
        p['policy']['costs'] = {'A': 2, 'B': .5, 'C': .5, 'D': 1.5, 'AB': 1, 'BC': .5}
        r = analyze(p)
        best = None
        # Separate oracle enumerates supported sets, not the solver's activity subsets.
        volumes = {'A': 500, 'B': 0, 'C': 200, 'D': 900, 'AB': 400, 'BC': 50}
        identities = sorted(volumes)
        for mask in range(1 << len(identities)):
            support = {i for n, i in enumerate(identities) if mask & (1 << n)}
            if ('AB' in support and not {'A', 'B'} <= support) or ('BC' in support and not {'B', 'C'} <= support):
                continue
            cost = sum(p['policy']['costs'][i] for i in sorted(support))
            if cost > 4:
                continue
            score = sum(volumes[i] for i in support if i in ('A', 'B', 'C', 'D')) / 1600 + sum(volumes[i] for i in support if i in ('AB', 'BC')) / 450
            key = (-score, cost, len(support), tuple(sorted(support)))
            if best is None or key < best[0]:
                best = key, support
        self.assertEqual(set(r['selection']['solution']['support']), best[1])
        self.assertAlmostEqual(r['selection']['solution']['score'], -best[0][0])

    def test_target_zero_infeasible_and_unknown_definition(self):
        p = self.base()
        p['policy'] = {'mode': 'target', 'targets': {'spreads': .8}, 'require_spread_legs': True}
        r = analyze(p)
        self.assertEqual(r['selection']['solution']['support'], ['A', 'AB', 'B'])
        self.assertEqual(r['selection']['solution']['cost'], 3)
        p['policy']['shared_budget'] = 2
        p['policy']['weights'] = {'outrights': 1, 'spreads': 1}
        self.assertEqual(analyze(p)['selection_status'], 'infeasible')
        p['policy'] = {'mode': 'budget', 'budgets': {'outrights': 0, 'spreads': 0}}
        self.assertEqual(analyze(p)['selected_instruments'], [])
        p['volume_basis'] = 'unknown'
        p['policy'] = {'mode': 'target', 'targets': {'outrights': .5}}
        self.assertEqual(analyze(p)['selection_status'], 'insufficient_data')
        self.assertEqual(analyze(p)['optimality'], 'not_solved')

    def test_heuristic_and_not_solved(self):
        p = self.base()
        p['policy']['exact_search_limit'] = 1
        r = analyze(p)
        self.assertEqual(r['optimality'], 'not_solved')
        self.assertTrue(all(x['action'] != 'support' for x in r['recommendations']))
        p['policy']['allow_heuristic'] = True
        self.assertEqual(analyze(p)['optimality'], 'heuristic')

    def test_pit_future_revision_and_forward(self):
        p = self.base()
        p['policy']['analysis_date'] = p['sessions'][1]
        p['policy']['decision_time'] = p['sessions'][1] + 'T23:30:00Z'
        revised = dict(p['daily'][0], volume=999999, available_at='2025-03-28T23:00:00Z')
        p['daily'].append(revised)
        r = analyze(p)
        self.assertEqual(r['instrument_metrics']['A']['total_known_volume'], 200)
        self.assertEqual(r['point_in_time']['status'], 'verified_against_supplied_availability')
        self.assertEqual(len(r['forward_validation']), 3)
        self.assertEqual(r['forward_validation'][0]['categories']['outrights']['known_instrument_count'], 4)
        p['policy'].pop('decision_time')
        p['revision_rule'] = 'latest_available'
        r = analyze(p)
        self.assertEqual(r['point_in_time']['status'], 'unverified')
        self.assertGreater(r['instrument_metrics']['A']['total_known_volume'], 999999)

    def test_mapping_formats_units_and_revisions(self):
        p = self.base()
        p['field_mapping'] = {'catalog': {'instrument_id': 'symbol'}, 'daily': {'instrument_id': 'symbol', 'volume': 'qty'}}
        p['normalization'] = {'thousands_separator': ',', 'null_markers': ['NA'], 'unit_conversions': {'mini': .1}}
        for row in p['instruments']:
            row['symbol'] = row.pop('instrument_id')
        for row in p['daily']:
            row['symbol'] = row.pop('instrument_id')
            row['qty'] = str(row.pop('volume'))
        p['daily'][0]['qty'] = '1,000'
        p['daily'][0]['source_volume_unit'] = 'mini'
        self.assertEqual(analyze(p)['instrument_metrics']['A']['total_known_volume'], 500)
        p['daily'].append(dict(p['daily'][0]))
        with self.assertRaisesRegex(ValidationError, 'duplicate observations'):
            analyze(p)

    def test_validation_errors(self):
        cases = [
            (lambda p: p['daily'][0].update(volume=-1), 'nonnegative'),
            (lambda p: p['daily'][0].update(volume=float('nan')), 'finite'),
            (lambda p: p['instruments'][0].update(listing_date='06/07/2025'), 'invalid date'),
            (lambda p: p['instruments'][0].update(listing_date='2026-01-01'), 'listing_date'),
            (lambda p: p['daily'][0].update(oi_date='2026-01-01'), 'oi_date'),
            (lambda p: p['instruments'][-1].update(front_leg_id='MISSING'), 'missing cataloged'),
            (lambda p: p['instruments'][-1].update(front_ratio=2), 'one-for-one'),
            (lambda p: p['instruments'][-1].update(family_id='OTHER'), 'cross-family'),
            (lambda p: p['instruments'][-1].update(front_leg_id='C', back_leg_id='B'), 'delivery month'),
            (lambda p: p['daily'][0].update(volume_unit='barrels'), 'volume unit'),
            (lambda p: p['sessions'].append(p['sessions'][-1]), 'ordered, unique'),
        ]
        for mutate, expected in cases:
            with self.subTest(expected=expected):
                p = self.base()
                mutate(p)
                with self.assertRaisesRegex(ValidationError, expected):
                    analyze(p)

    def test_policy_required_oi_and_eligibility(self):
        p = self.base()
        del p['policy']
        self.assertEqual(analyze(p)['selection_status'], 'policy_required')
        p['policy'] = {'mode': 'budget', 'budgets': {'outrights': 2}, 'min_oi': 10, 'max_oi_age_days': 1}
        r = analyze(p)
        self.assertEqual(r['selected_instruments'], ['A'])
        self.assertEqual(self.rec(r, 'D')['label'], 'insufficient_data')
        p['policy']['require_delivery_eligible'] = True
        self.assertEqual(analyze(p)['selected_instruments'], [])
        p['instruments'][0]['delivery_eligible'] = True
        self.assertEqual(analyze(p)['selected_instruments'], ['A'])

    def test_snapshots_alternative(self):
        p = self.base()
        catalog = p.pop('instruments')
        p['instrument_snapshots'] = [{'trade_date': p['sessions'][0], 'complete': True,
                                      'instruments': catalog}]
        r = analyze(p)
        self.assertEqual(r['selected_instruments'], ['A', 'AB', 'D'])
        self.assertEqual(r['date_mappings'][-1]['snapshot_source_date'], p['sessions'][0])
        self.assertEqual(r['point_in_time']['status'], 'unverified')

    def test_shared_leg_counted_once_and_capacity_dimensions(self):
        p = self.base()
        p['policy']['shared_budget'] = 6
        r = analyze(p)
        self.assertEqual(r['selected_instruments'], ['A', 'AB', 'BC', 'C', 'D'])
        self.assertEqual(r['supporting_legs'], ['B'])
        self.assertEqual(r['selection']['solution']['cost'], 6)
        p['policy']['capacities'] = {'connections': 1}
        p['policy']['resource_usage'] = {'A': {'connections': 1}, 'D': {'connections': 1}}
        r = analyze(p)
        self.assertLessEqual(r['selection']['solution']['resource_usage']['connections'], 1)
        self.assertFalse({'A', 'D'} <= set(r['selection']['solution']['support']))

    def test_ties_determinism_and_no_mutation(self):
        p = self.base()
        p['policy'] = {'mode': 'budget', 'budgets': {'outrights': 1}}
        for row in p['daily']:
            if row['instrument_id'] in ('A', 'D'):
                row['volume'] = 100
        original = copy.deepcopy(p)
        r = analyze(p)
        self.assertEqual(p, original)
        self.assertEqual(r['selected_instruments'], ['A'])
        p['daily'].reverse()
        p['instruments'].reverse()
        self.assertEqual(analyze(p), r)

    def test_large_independent_chain_proven_without_exponential_search(self):
        p = self.base()
        p['policy'] = {'mode': 'budget', 'budgets': {'outrights': 2}, 'exact_search_limit': 1}
        r = analyze(p)
        self.assertEqual(r['selected_instruments'], ['A', 'D'])
        self.assertEqual(r['optimality'], 'proven')
        self.assertIn('prefixes', r['selection']['reason'])

    def test_same_product_code_does_not_merge_spread_instruments(self):
        p = self.base()
        item = dict(p['instruments'][-2], instrument_id='AB2')
        p['instruments'].append(item)
        p['daily'] += [dict(row, instrument_id='AB2') for row in p['daily'] if row['instrument_id'] == 'AB']
        r = analyze(p)
        buckets = [b for b in r['relative_metrics'] if b['relative_label'] == 'S(1,2)']
        self.assertEqual({b['instrument_id'] for b in buckets}, {'AB', 'AB2'})

    def test_metadata_and_eligibility_availability(self):
        p = self.base()
        revised = dict(p['instruments'][0], expiry_date='2025-12-31', available_at='2025-04-01T00:00:00Z')
        p['instruments'].append(revised)
        p['instruments'][0]['eligibility'] = [{'date': '2025-03-24', 'delivery_eligible': False,
                                             'available_at': '2025-04-01T00:00:00Z'}]
        r = analyze(p)
        self.assertEqual(r['date_mappings'][0]['outrights'][0]['instrument_id'], 'A')
        self.assertIn('A', r['selected_instruments'])
        p['policy'].pop('decision_time')
        with self.assertRaisesRegex(ValidationError, 'duplicate catalog'):
            analyze(p)
        p['revision_rule'] = 'latest_available'
        self.assertEqual(analyze(p)['date_mappings'][0]['outrights'][0]['instrument_id'], 'B')

    def test_stale_oi_notice_and_incomplete_defaults(self):
        p = self.base()
        p['daily'][0]['oi_date'] = '2025-03-01'
        for row in p['daily'][1:]:
            row['open_interest'] = None
        p['policy']['min_oi'] = 1
        p['policy']['max_oi_age_days'] = 5
        self.assertEqual(self.rec(analyze(p), 'A')['label'], 'insufficient_data')
        p = self.base()
        p['policy']['exclude_on_or_after_first_notice'] = True
        p['instruments'][0]['first_notice_date'] = '2025-03-28'
        r = analyze(p)
        self.assertEqual(self.rec(r, 'A')['action'], 'ineligible')
        p = self.base()
        p.pop('metadata')
        r = analyze(p)
        self.assertTrue(r['selected_instruments'])
        self.assertEqual(r['category_denominators']['outrights']['scope'], 'supplied_observed_universe')
        self.assertTrue(r['limitations'])

    def test_explicit_revisions_and_conflicting_sources(self):
        p = self.base()
        p['revision_rule'] = 'latest_available'
        p['daily'].append(dict(p['daily'][0], volume=200, available_at='2025-03-25T01:00:00Z'))
        r = analyze(p)
        self.assertEqual(r['instrument_metrics']['A']['total_known_volume'], 600)
        self.assertEqual(len(r['diagnostics']['resolved_revisions']), 1)
        p['daily'][-1]['source'] = 'different_vendor'
        with self.assertRaisesRegex(ValidationError, 'conflicting sources'):
            analyze(p)

    def test_category_adv_aggregates_daily_contracts(self):
        p = self.base()
        r = analyze(p)
        category = r['category_denominators']['outrights']
        self.assertEqual(category['adv'], 320)
        self.assertEqual(category['known_sessions'], 5)
        self.assertEqual(category['expected_instrument_sessions'], 20)
        p['daily'][0]['volume'] = None
        category = analyze(p)['category_denominators']['outrights']
        self.assertEqual(category['total_known_volume'], 1500)
        self.assertEqual(category['adv'], 320)
        self.assertEqual(category['known_sessions'], 4)
        self.assertEqual(category['missing_instrument_sessions'], 1)

    def test_target_caps_are_validated_and_applied(self):
        p = self.base()
        p['policy'] = {'mode': 'target', 'targets': {'outrights': .5}, 'budgets': {'outrights': -1}}
        with self.assertRaisesRegex(ValidationError, 'nonnegative'):
            analyze(p)
        p['policy']['budgets']['outrights'] = 1
        r = analyze(p)
        self.assertEqual(r['selected_instruments'], ['D'])
        self.assertEqual(r['selection']['solution']['cost'], 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--input', help='Canonical JSON batch (vendor field mapping supported)')
    parser.add_argument('--output', help='JSON report; defaults to stdout')
    parser.add_argument('--csv', help='Write recommendations CSV')
    parser.add_argument('--example', action='store_true', help='Emit labeled synthetic canonical input')
    parser.add_argument('--self-test', action='store_true', help='Run embedded unittest suite')
    parser.add_argument('--summary', action='store_true', help='Print readable selection summary to stderr')
    parser.add_argument('--policy', help='JSON policy file; replaces input policy before individual overrides')
    parser.add_argument('--mode', choices=['budget', 'target'])
    parser.add_argument('--analysis-date', help='Last decision exchange trading day, YYYY-MM-DD')
    parser.add_argument('--start-date', help='First included trading day; takes precedence over lookback')
    parser.add_argument('--decision-time', help='Point-in-time availability cutoff, ISO timestamp with offset')
    parser.add_argument('--lookback-sessions', type=int)
    parser.add_argument('--outright-budget', type=float)
    parser.add_argument('--spread-budget', type=float)
    parser.add_argument('--shared-budget', type=float, help='Overall support cost; requires explicit category weights')
    parser.add_argument('--outright-weight', type=float)
    parser.add_argument('--spread-weight', type=float)
    parser.add_argument('--outright-target', type=float)
    parser.add_argument('--spread-target', type=float)
    parser.add_argument('--min-adv', type=float)
    parser.add_argument('--min-positive-frequency', type=float)
    parser.add_argument('--min-data-completeness', type=float)
    parser.add_argument('--require-spread-legs', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--exact-search-limit', type=int)
    parser.add_argument('--allow-heuristic', action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CoverageTests))
            return 0 if result.wasSuccessful() else 1
        if args.example:
            data = synthetic_example()
        else:
            if not args.input:
                parser.error('provide --input, --example, or --self-test')
            with open(args.input, encoding='utf-8') as handle:
                data = json.load(handle)
            if args.policy:
                with open(args.policy, encoding='utf-8') as handle:
                    data['policy'] = json.load(handle)
            scalar = ['mode', 'analysis_date', 'start_date', 'decision_time', 'lookback_sessions', 'shared_budget',
                      'min_adv', 'min_positive_frequency', 'min_data_completeness', 'require_spread_legs',
                      'exact_search_limit', 'allow_heuristic']
            overrides = {k: getattr(args, k) for k in scalar if getattr(args, k) is not None}
            for suffix, key in [('budget', 'budgets'), ('target', 'targets'), ('weight', 'weights')]:
                for category, prefix in [('outrights', 'outright'), ('spreads', 'spread')]:
                    value = getattr(args, f'{prefix}_{suffix}')
                    if value is not None:
                        overrides.setdefault(key, {})[category] = value
            if overrides:
                policy = data.setdefault('policy', {})
                for key, value in overrides.items():
                    if isinstance(value, dict):
                        policy.setdefault(key, {}).update(value)
                    else:
                        policy[key] = value
                if 'mode' not in policy:
                    policy['mode'] = 'target' if 'targets' in policy else 'budget'
            data = analyze(data)
        encoded = json.dumps(data, indent=2, allow_nan=False) + '\n'
        if args.output:
            Path(args.output).write_text(encoded, encoding='utf-8')
        else:
            sys.stdout.write(encoded)
        if args.csv:
            if args.example:
                fail('--csv requires analysis; save --example input and analyze it first')
            export_csv(data, args.csv)
        if args.summary and not args.example:
            print(summary(data), file=sys.stderr)
        return 0
    except (ValidationError, OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
