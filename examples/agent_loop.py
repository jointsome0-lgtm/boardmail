#!/usr/bin/env python3
"""Print local arrivals as JSON lines and persist the delivery checkpoint.

Run collect separately. This example's handling step is printing, not replying.
Replace deliver() with your agent's completed work before advancing the checkpoint.
An optional ledger records delivery attempts and explicitly supplied outcomes.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


OUTCOMES = ('acted', 'resolved', 'escalated', 'ignored', 'unrecorded')


def deliver(message, reading):
    print(json.dumps(message, ensure_ascii=True), flush=True)
    # Return a recorded outcome only after your handler has established it.
    return 'unrecorded'


def delivered_chars(message):
    """Unicode characters in delivered titles/bodies, excluding JSON and metadata."""
    brief = message.get('brief', {})
    parts = [message, brief.get('root', {}), brief.get('parent', {})]
    parts.extend(brief.get('previous_exchange', {}).get('messages', []))
    return sum(len(part.get(field, '')) for part in parts for field in ('title', 'body'))


def load_ledger(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def append_ledger(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry, ensure_ascii=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def summarize(entries):
    def totals(records):
        deliveries = [row for row in records if row['event'] == 'delivery']
        latest = {(row['source'], row['id']): 'unrecorded' for row in deliveries}
        for row in records:
            if row['event'] in ('delivery', 'outcome') and row['outcome'] != 'unrecorded':
                key = row['source'], row['id']
                if key not in latest or row['outcome'] not in OUTCOMES:
                    raise ValueError('Outcome requires a delivered message and a supported value')
                latest[key] = row['outcome']
        return {'unique_messages': len(latest), 'delivery_attempts': len(deliveries),
                'summary_attempts': sum(row['event'] == 'thread_activity' for row in records),
                'delivered_chars': sum(row['delivered_chars'] for row in deliveries),
                'outcomes': {name: sum(value == name for value in latest.values()) for name in OUTCOMES}}

    groups = {}
    for row in entries:
        reading = row['reading']
        groups.setdefault(reading['scope'] + '/' + reading['context'], []).append(row)
    return {'event': 'ledger_summary', **totals(entries),
            'by_reading': {key: totals(rows) for key, rows in groups.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--once", action="store_true", help="Drain current arrivals, then exit")
    parser.add_argument("--ledger", type=Path, help="Append local delivery observations without message text")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--summarize", action="store_true", help="Summarize an existing ledger, without reading the inbox")
    mode.add_argument("--record-outcome", nargs=2, metavar=('SOURCE', 'ID'),
                      help="Record an explicit outcome for the message's most recent delivery attempt")
    parser.add_argument("--outcome", choices=OUTCOMES[:-1])
    parser.add_argument("--ref", help="Optional evidence reference for the explicit outcome; never fetched")
    args = parser.parse_args(argv)
    if args.summarize or args.record_outcome:
        if not args.ledger or not args.ledger.is_file():
            parser.error('This mode requires an existing --ledger file')
        if args.db or args.checkpoint or args.once:
            parser.error('Ledger-only modes do not take --db, --checkpoint or --once')
        entries = load_ledger(args.ledger)
        if args.summarize:
            if args.outcome or args.ref:
                parser.error('--outcome and --ref require --record-outcome')
            print(json.dumps(summarize(entries), ensure_ascii=True))
            return 0
        if not args.outcome:
            parser.error('--record-outcome requires --outcome')
        source, message_id = args.record_outcome
        delivered = [row for row in entries if row['event'] == 'delivery'
                     and (row['source'], row['id']) == (source, message_id)]
        if not delivered:
            parser.error('The source and ID have no recorded delivery')
        last = delivered[-1]
        entry = {'event': 'outcome', 'at': datetime.now(timezone.utc).isoformat(),
                 'source': source, 'id': message_id, 'attempt': last['attempt'],
                 'reading': last['reading'], 'outcome': args.outcome, 'ref': args.ref}
        append_ledger(args.ledger, entry)
        print(json.dumps(entry, ensure_ascii=True))
        return 0
    if not args.db or not args.checkpoint:
        parser.error('Delivery requires --db and --checkpoint')
    if args.outcome or args.ref:
        parser.error('--outcome and --ref require --record-outcome')
    entries = load_ledger(args.ledger) if args.ledger and args.ledger.exists() else []
    attempts = Counter((row['source'], row['id']) for row in entries if row['event'] == 'delivery')
    after = int(args.checkpoint.read_text()) if args.checkpoint.exists() else 0
    while True:
        result = subprocess.run([sys.executable, "-m", "boardmail", "--db", args.db,
            "wait", "--after", str(after), "--timeout", "0" if args.once else "60"],
            capture_output=True, text=True)
        value = json.loads(result.stdout)
        if result.returncode not in (0, 3):
            print(json.dumps(value), file=sys.stderr)
            return result.returncode
        if value["event"] == "timeout":
            print(json.dumps({"event": "timeout", "sources": value["sources"],
                              "history_complete": False}), file=sys.stderr)
            if args.once: return 0
            continue
        if value['event'] != 'messages' or not value.get('checkpoint_safe'):
            raise ValueError('A delivery checkpoint requires an unfiltered messages page')
        # Both arrays belong to this page. A failed handler leaves its checkpoint unchanged.
        for message in value["messages"]:
            outcome = deliver(message, value['reading'])
            if outcome not in OUTCOMES:
                raise ValueError('The handler returned an unsupported outcome')
            if args.ledger:
                key = message['source'], message['id']
                attempts[key] += 1
                append_ledger(args.ledger, {'event': 'delivery', 'at': datetime.now(timezone.utc).isoformat(),
                    'source': key[0], 'id': key[1], 'arrival_seq': message['arrival_seq'],
                    'attempt': attempts[key], 'reading': value['reading'],
                    'delivered_chars': delivered_chars(message), 'outcome': outcome})
        for activity in value.get("thread_activity", []):
            deliver({"event": "thread_activity", **activity}, value['reading'])
            if args.ledger:
                append_ledger(args.ledger, {'event': 'thread_activity', 'at': datetime.now(timezone.utc).isoformat(),
                    **{key: activity[key] for key in ('source', 'thread_id', 'count', 'first_seq', 'last_seq')},
                    'reading': value['reading']})
        after = value["next_after"]
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.checkpoint.with_name(args.checkpoint.name+".tmp")
        temporary.write_text(str(after)+"\n")
        temporary.replace(args.checkpoint)


if __name__ == "__main__": raise SystemExit(main())
